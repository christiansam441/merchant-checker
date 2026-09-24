import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.fetcher import FetchedPage
from app.models import ChecklistEvidence, ChecklistItem, ProcessorResult


DEFAULT_CHECKLIST_RULES_PATH = Path(__file__).resolve().parent / "data" / "checklist_rules.json"
ChecklistSignalType = Literal[
    "link_text",
    "url",
    "page_text",
    "email",
    "phone",
    "address",
    "script_src",
    "html",
    "meta_generator",
    "header",
    "cookie",
]


class ChecklistRulesValidationError(ValueError):
    pass


class ChecklistSignal(BaseModel):
    type: ChecklistSignalType
    pattern: str
    evidence_label: str
    weight: int = Field(default=1, ge=1, le=100)

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, pattern: str) -> str:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return pattern


class ChecklistRule(BaseModel):
    key: str
    name: str
    signals: list[ChecklistSignal] = Field(min_length=1)


class PlatformRule(BaseModel):
    name: str
    signals: list[ChecklistSignal] = Field(min_length=1)


class DerivedRule(BaseModel):
    key: Literal["ecommerce_platform", "multiple_processors"]
    name: str
    evidence_label: str


class ChecklistRules(BaseModel):
    crawl_keywords: list[str] = Field(min_length=1)
    checklist_items: list[ChecklistRule] = Field(min_length=1)
    platforms: list[PlatformRule] = Field(min_length=1)
    derived_items: list[DerivedRule] = Field(min_length=2)


def load_checklist_rules(path: Path = DEFAULT_CHECKLIST_RULES_PATH) -> ChecklistRules:
    try:
        raw_rules = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChecklistRulesValidationError(f"Checklist rules file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ChecklistRulesValidationError(
            f"Checklist rules contain invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    try:
        return ChecklistRules.model_validate(raw_rules)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ChecklistRulesValidationError(f"Invalid checklist rules in {path}: {problems}") from exc


def _signal_candidates(
    signal_type: ChecklistSignalType,
    page: FetchedPage,
    soup: BeautifulSoup,
) -> list[str]:
    if signal_type == "link_text":
        return [link.get_text(" ", strip=True) for link in soup.find_all("a")]
    if signal_type == "url":
        return [
            urljoin(page.url, href)
            for link in soup.find_all("a", href=True)
            if isinstance((href := link.get("href")), str)
        ]
    if signal_type in {"page_text", "email", "phone", "address"}:
        values = [soup.get_text(" ", strip=True)]
        if signal_type == "email":
            values.extend(
                href.removeprefix("mailto:").split("?", 1)[0]
                for link in soup.find_all("a", href=True)
                if isinstance((href := link.get("href")), str) and href.casefold().startswith("mailto:")
            )
        if signal_type == "phone":
            values.extend(
                href.removeprefix("tel:")
                for link in soup.find_all("a", href=True)
                if isinstance((href := link.get("href")), str) and href.casefold().startswith("tel:")
            )
        return values
    if signal_type == "script_src":
        return [
            urljoin(page.url, src)
            for script in soup.find_all("script", src=True)
            if isinstance((src := script.get("src")), str)
        ]
    if signal_type == "html":
        return [page.html]
    if signal_type == "meta_generator":
        return [
            content
            for meta in soup.find_all("meta")
            if str(meta.get("name", "")).casefold() == "generator"
            and isinstance((content := meta.get("content")), str)
        ]
    if signal_type == "header":
        return [f"{name}: {value}" for name, value in page.headers.items()]
    if signal_type == "cookie":
        return list(page.cookies)
    return []


def _match_signal(
    signal: ChecklistSignal,
    pages: list[FetchedPage],
) -> list[ChecklistEvidence]:
    pattern = re.compile(signal.pattern)
    evidence = []
    for page in pages:
        soup = BeautifulSoup(page.html, "html.parser")
        for candidate in _signal_candidates(signal.type, page, soup):
            match = pattern.search(candidate)
            if not match:
                continue
            evidence.append(
                ChecklistEvidence(
                    label=signal.evidence_label,
                    matched_value=match.group(0),
                    source_page=page.url,
                )
            )
            break
    return evidence


def detect_platform(
    pages: list[FetchedPage],
    rules: ChecklistRules,
) -> tuple[str | None, list[ChecklistEvidence]]:
    matches = []
    for platform in rules.platforms:
        platform_evidence = []
        score = 0
        for signal in platform.signals:
            signal_evidence = _match_signal(signal, pages)
            if signal_evidence:
                score += signal.weight
                platform_evidence.extend(signal_evidence)
        if platform_evidence:
            matches.append((score, platform.name, platform_evidence))

    if not matches:
        return None, []
    _, name, evidence = max(matches, key=lambda item: item[0])
    return name, evidence


def build_checklist(
    pages: list[FetchedPage],
    processors: list[ProcessorResult],
    rules: ChecklistRules,
) -> tuple[list[ChecklistItem], str | None]:
    items = []
    for rule in rules.checklist_items:
        evidence = []
        for signal in rule.signals:
            evidence.extend(_match_signal(signal, pages))
        items.append(
            ChecklistItem(
                key=rule.key,
                name=rule.name,
                passed=bool(evidence),
                evidence=evidence,
            )
        )

    platform, platform_evidence = detect_platform(pages, rules)
    derived = {rule.key: rule for rule in rules.derived_items}
    platform_rule = derived["ecommerce_platform"]
    items.append(
        ChecklistItem(
            key=platform_rule.key,
            name=platform_rule.name,
            passed=platform is not None,
            evidence=(
                [
                    ChecklistEvidence(
                        label=platform_rule.evidence_label,
                        matched_value=platform,
                        source_page=platform_evidence[0].source_page,
                    )
                ]
                if platform and platform_evidence
                else []
            ),
        )
    )

    processor_rule = derived["multiple_processors"]
    processor_evidence = [
        ChecklistEvidence(
            label=processor_rule.evidence_label,
            matched_value=processor.name,
            source_page=processor.evidence[0].source_page,
        )
        for processor in processors
        if processor.evidence
    ]
    items.append(
        ChecklistItem(
            key=processor_rule.key,
            name=processor_rule.name,
            passed=len(processors) > 1,
            evidence=processor_evidence if len(processors) > 1 else [],
        )
    )
    return items, platform
