"""Shared agent contracts, independent of any CLI module identity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Union


MAX_REPORT_TEXT_CHARS = 1_000


class RootCauseCategory(str, Enum):
    SCHEMA_DRIFT = "schema_drift"
    DATA_QUALITY = "data_quality"
    JOIN_REFERENCE = "join_reference"
    FRESHNESS_VOLUME = "freshness_volume"
    UNKNOWN = "unknown"


class Uncertainty(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ToolRequest:
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class EvidenceReference:
    reference_id: str
    claim: str


@dataclass(frozen=True)
class ReportDraft:
    root_cause_category: RootCauseCategory
    explanation: str
    evidence_references: tuple[EvidenceReference, ...]
    proposed_human_action: str
    uncertainty: Uncertainty

    def __post_init__(self) -> None:
        if not isinstance(self.root_cause_category, RootCauseCategory):
            raise ValueError("root_cause_category is not allowed")
        if not isinstance(self.uncertainty, Uncertainty):
            raise ValueError("uncertainty is not allowed")
        for field_name in ("explanation", "proposed_human_action"):
            value = getattr(self, field_name)
            if not value or len(value) > MAX_REPORT_TEXT_CHARS:
                raise ValueError(f"{field_name} must contain 1-{MAX_REPORT_TEXT_CHARS} characters")
        combined = f"{self.explanation} {self.proposed_human_action}".lower()
        if any(phrase in combined for phrase in ("i fixed", "fix was applied", "changes were made", "executed the fix")):
            raise ValueError("report must not claim that a change was executed")
        if not self.evidence_references:
            raise ValueError("at least one evidence reference is required")
        for reference in self.evidence_references:
            if not reference.reference_id or not reference.claim or len(reference.claim) > 300:
                raise ValueError("evidence references require a short ID and claim")


@dataclass(frozen=True)
class IncidentReport:
    run_id: str
    root_cause_category: RootCauseCategory
    explanation: str
    evidence_references: tuple[EvidenceReference, ...]
    proposed_human_action: str
    uncertainty: Uncertainty
    changes_made: bool = False


@dataclass(frozen=True)
class ToolObservation:
    reference_id: str
    tool: str
    arguments: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None


ModelDecision = Union[ToolRequest, ReportDraft]


def jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    return value
