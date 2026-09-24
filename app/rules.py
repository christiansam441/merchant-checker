import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.models import SignalType


DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "data" / "detection_rules.json"


class RulesValidationError(ValueError):
    pass


class SignalRule(BaseModel):
    type: SignalType
    pattern: str
    weight: int = Field(ge=1, le=100)
    evidence_label: str

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, pattern: str) -> str:
        if not pattern.strip():
            raise ValueError("pattern must not be empty")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return pattern

    @field_validator("evidence_label")
    @classmethod
    def validate_evidence_label(cls, label: str) -> str:
        if not label.strip():
            raise ValueError("evidence_label must not be empty")
        return label


class ProcessorRule(BaseModel):
    name: str
    signals: list[SignalRule] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("name must not be empty")
        return name


class DetectionRules(BaseModel):
    processors: list[ProcessorRule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_names(self) -> "DetectionRules":
        names = [processor.name.casefold() for processor in self.processors]
        if len(names) != len(set(names)):
            raise ValueError("processor names must be unique")
        return self


def load_rules(path: Path = DEFAULT_RULES_PATH) -> DetectionRules:
    try:
        raw_rules = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RulesValidationError(f"Detection rules file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RulesValidationError(
            f"Detection rules contain invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    try:
        return DetectionRules.model_validate(raw_rules)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise RulesValidationError(f"Invalid detection rules in {path}: {problems}") from exc
