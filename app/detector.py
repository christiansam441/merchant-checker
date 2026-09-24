import re
from collections.abc import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.fetcher import FetchedPage
from app.models import Evidence, ProcessorResult, SignalType
from app.rules import DetectionRules, SignalRule


STRONG_SIGNAL_TYPES = {"script_src", "iframe_src", "form_action", "link_href"}


def _attribute_values(soup: BeautifulSoup, tag: str, attribute: str, base_url: str) -> list[str]:
    values = []
    for element in soup.find_all(tag):
        value = element.get(attribute)
        if isinstance(value, str) and value.strip():
            values.append(urljoin(base_url, value.strip()))
    return values


def _javascript_globals(soup: BeautifulSoup) -> list[str]:
    candidates: set[str] = set()
    explicit_global = re.compile(
        r"\b(?:window|globalThis)\.([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    )
    declared_global = re.compile(
        r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*="
    )

    for script in soup.find_all("script", src=False):
        script_text = script.get_text(" ")
        tokens = [*explicit_global.findall(script_text), *declared_global.findall(script_text)]
        for token in tokens:
            parts = token.split(".")
            candidates.update(".".join(parts[:index]) for index in range(1, len(parts) + 1))

    return sorted(candidates)


def _candidates_for_signal(
    signal_type: SignalType,
    page: FetchedPage,
    soup: BeautifulSoup,
    js_globals: list[str],
) -> Iterable[str]:
    if signal_type == "script_src":
        return _attribute_values(soup, "script", "src", page.url)
    if signal_type == "iframe_src":
        return _attribute_values(soup, "iframe", "src", page.url)
    if signal_type == "form_action":
        return _attribute_values(soup, "form", "action", page.url)
    if signal_type == "link_href":
        return _attribute_values(soup, "a", "href", page.url)
    if signal_type == "js_global":
        return js_globals
    if signal_type == "cookie":
        return page.cookies.keys()
    if signal_type == "header":
        return (f"{name}: {value}" for name, value in page.headers.items())
    if signal_type == "page_text":
        return [soup.get_text(" ", strip=True)]
    return []


def _match_signal(
    signal: SignalRule,
    page: FetchedPage,
    soup: BeautifulSoup,
    js_globals: list[str],
) -> str | None:
    pattern = re.compile(signal.pattern)
    for candidate in _candidates_for_signal(signal.type, page, soup, js_globals):
        match = pattern.search(candidate)
        if match:
            if signal.type == "page_text":
                return match.group(0)
            return candidate
    return None


def is_strong_evidence(evidence: Evidence) -> bool:
    return evidence.signal_type in STRONG_SIGNAL_TYPES


def confidence_for_evidence(evidence: list[Evidence]) -> str:
    if not any(is_strong_evidence(item) for item in evidence):
        return "low"
    total_weight = sum(item.strength for item in evidence)
    if total_weight >= 80:
        return "high"
    if total_weight >= 50:
        return "medium"
    return "low"


def detect_processors(page: FetchedPage, rules: DetectionRules) -> list[ProcessorResult]:
    soup = BeautifulSoup(page.html, "html.parser")
    js_globals = _javascript_globals(soup)
    results = []

    for processor in rules.processors:
        evidence = []

        for signal in processor.signals:
            matched_value = _match_signal(signal, page, soup, js_globals)
            if matched_value is None:
                continue
            evidence.append(
                Evidence(
                    signal_type=signal.type,
                    matched_value=matched_value,
                    strength=signal.weight,
                    label=signal.evidence_label,
                    source_page=page.url,
                )
            )

        if evidence:
            results.append(
                ProcessorResult(
                    name=processor.name,
                    confidence=confidence_for_evidence(evidence),
                    evidence=evidence,
                )
            )

    return results
