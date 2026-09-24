import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from app.checklist import ChecklistRules, build_checklist
from app.detector import confidence_for_evidence, detect_processors, is_strong_evidence
from app.fetcher import FetchError, FetchedPage, TargetSiteBlocked, fetch_page, validate_public_url
from app.models import Evidence, ProcessorResult, ScanEvent, ScanResult
from app.rules import DetectionRules


COMMERCE_KEYWORDS = (
    "checkout",
    "cart",
    "pricing",
    "subscribe",
    "donate",
    "shop",
    "plans",
    "buy",
    "order",
    "store",
)
NON_HTML_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".tar",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
MAX_COMMERCE_PAGES = 5
MAX_TOTAL_PAGES = 8
SCAN_TIMEOUT_SECONDS = 30

FetchPage = Callable[[str], Awaitable[FetchedPage]]


def _looks_like_html_page(path: str) -> bool:
    final_segment = path.rsplit("/", 1)[-1].casefold()
    return not any(final_segment.endswith(extension) for extension in NON_HTML_EXTENSIONS)


def find_commerce_links(page: FetchedPage, limit: int = MAX_COMMERCE_PAGES) -> list[str]:
    return _find_ranked_links(page, COMMERCE_KEYWORDS, limit)


def _find_ranked_links(
    page: FetchedPage,
    keywords: tuple[str, ...] | list[str],
    limit: int,
) -> list[str]:
    soup = BeautifulSoup(page.html, "html.parser")
    home = urlparse(page.url)
    home_url = urldefrag(page.url).url
    ranked: dict[str, tuple[int, int]] = {}

    for position, link in enumerate(soup.find_all("a", href=True)):
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            continue

        absolute = urldefrag(urljoin(page.url, href.strip())).url
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if (parsed.hostname or "").casefold() != (home.hostname or "").casefold():
            continue
        if absolute == home_url or not _looks_like_html_page(parsed.path):
            continue

        searchable = f"{parsed.path} {parsed.query} {link.get_text(' ', strip=True)}".casefold()
        score = sum(1 for keyword in keywords if keyword.casefold() in searchable)
        if score == 0:
            continue

        previous = ranked.get(absolute)
        if previous is None or score > previous[0]:
            ranked[absolute] = (score, position)

    ordered = sorted(ranked, key=lambda url: (-ranked[url][0], ranked[url][1]))
    return ordered[:limit]


def find_scan_links(page: FetchedPage, checklist_rules: ChecklistRules) -> list[str]:
    limit = MAX_TOTAL_PAGES - 1
    commerce_links = _find_ranked_links(page, COMMERCE_KEYWORDS, MAX_COMMERCE_PAGES)
    policy_links = _find_ranked_links(page, checklist_rules.crawl_keywords, limit)
    combined = []
    for index in range(max(len(commerce_links), len(policy_links))):
        for links in (policy_links, commerce_links):
            if index < len(links) and links[index] not in combined:
                combined.append(links[index])
    return combined[:limit]


def merge_processor_results(result_sets: list[list[ProcessorResult]]) -> list[ProcessorResult]:
    merged: dict[str, list[Evidence]] = {}
    seen: dict[str, set[tuple[str, ...]]] = {}

    for results in result_sets:
        for result in results:
            processor_evidence = merged.setdefault(result.name, [])
            processor_seen = seen.setdefault(result.name, set())
            for evidence in result.evidence:
                if is_strong_evidence(evidence):
                    identity = (
                        evidence.signal_type,
                        evidence.label,
                        evidence.matched_value.casefold(),
                    )
                else:
                    identity = (evidence.signal_type, evidence.label)
                if identity in processor_seen:
                    continue
                processor_seen.add(identity)
                processor_evidence.append(evidence)

    return [
        ProcessorResult(
            name=name,
            confidence=confidence_for_evidence(evidence),
            evidence=evidence,
        )
        for name, evidence in merged.items()
    ]


async def _scan_events(
    url: str,
    rules: DetectionRules,
    checklist_rules: ChecklistRules,
    fetch: FetchPage = fetch_page,
) -> AsyncIterator[ScanEvent]:
    yield ScanEvent(event="progress", message="Validating URL", progress=5)
    try:
        await validate_public_url(url)
    except FetchError as exc:
        yield ScanEvent(event="error", message=str(exc), progress=5)
        return

    yield ScanEvent(event="progress", message="Fetching homepage", progress=15)
    try:
        homepage = await fetch(url)
    except FetchError as exc:
        yield ScanEvent(
            event="error",
            message=str(exc),
            progress=15,
            error_category="blocked_by_site" if isinstance(exc, TargetSiteBlocked) else None,
        )
        return

    scan_links = find_scan_links(homepage, checklist_rules)
    yield ScanEvent(
        event="progress",
        message=f"Found {len(scan_links)} candidate commerce, policy, and contact pages",
        progress=25,
    )

    all_results = []
    fetched_pages = [homepage]
    yield ScanEvent(
        event="progress",
        message=f"Analyzing scripts and page signals on {homepage.url}",
        progress=35,
    )
    all_results.append(detect_processors(homepage, rules))

    page_count = len(scan_links)
    for index, page_url in enumerate(scan_links, start=1):
        progress = 35 + int((index - 1) * 55 / max(page_count, 1))
        yield ScanEvent(
            event="progress",
            message=f"Fetching commerce page {index} of {page_count}: {page_url}",
            progress=progress,
        )
        try:
            page = await fetch(page_url)
        except FetchError as exc:
            yield ScanEvent(
                event="progress",
                message=f"Skipped {page_url}: {exc}",
                progress=progress,
            )
            continue

        yield ScanEvent(
            event="progress",
            message=f"Analyzing scripts and page signals on {page.url}",
            progress=min(progress + 5, 90),
        )
        fetched_pages.append(page)
        all_results.append(detect_processors(page, rules))

    merged_results = merge_processor_results(all_results)
    processors = [result for result in merged_results if result.confidence != "low"]
    possible_mentions = [result for result in merged_results if result.confidence == "low"]
    yield ScanEvent(
        event="progress",
        message="Evaluating merchant checklist and ecommerce platform",
        progress=95,
    )
    checklist, ecommerce_platform = build_checklist(
        fetched_pages,
        processors,
        checklist_rules,
    )
    result = ScanResult(
        url=homepage.url,
        processors=processors,
        possible_mentions=possible_mentions,
        checklist=checklist,
        ecommerce_platform=ecommerce_platform,
    )
    yield ScanEvent(
        event="result",
        message="Scan complete",
        progress=100,
        result=result,
    )


async def scan_events(
    url: str,
    rules: DetectionRules,
    checklist_rules: ChecklistRules,
    fetch: FetchPage = fetch_page,
    timeout_seconds: float = SCAN_TIMEOUT_SECONDS,
) -> AsyncIterator[ScanEvent]:
    last_progress = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in _scan_events(url, rules, checklist_rules, fetch):
                last_progress = event.progress
                yield event
    except TimeoutError:
        yield ScanEvent(
            event="error",
            message=f"Scan stopped after the {timeout_seconds:g}-second time limit.",
            progress=last_progress,
        )
