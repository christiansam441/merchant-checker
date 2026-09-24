from typing import Literal

from pydantic import BaseModel, Field


SignalType = Literal[
    "script_src",
    "iframe_src",
    "form_action",
    "link_href",
    "js_global",
    "cookie",
    "header",
    "page_text",
]


class Evidence(BaseModel):
    signal_type: SignalType
    matched_value: str
    strength: int = Field(ge=1, le=100)
    label: str
    source_page: str


class ProcessorResult(BaseModel):
    name: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(default_factory=list)


class ChecklistEvidence(BaseModel):
    label: str
    matched_value: str
    source_page: str


class ChecklistItem(BaseModel):
    key: str
    name: str
    passed: bool
    evidence: list[ChecklistEvidence] = Field(default_factory=list)


class ScanResult(BaseModel):
    url: str
    processors: list[ProcessorResult] = Field(default_factory=list)
    possible_mentions: list[ProcessorResult] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    ecommerce_platform: str | None = None


class ScanEvent(BaseModel):
    event: Literal["progress", "result", "error"]
    message: str
    progress: int = Field(ge=0, le=100)
    error_category: Literal["blocked_by_site"] | None = None
    result: ScanResult | None = None
