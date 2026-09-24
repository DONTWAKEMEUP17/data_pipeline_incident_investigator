"""Deterministic content-based scoring for benchmark v2 reports."""

from __future__ import annotations

import json
from typing import Any

from .evaluation_cases_v2 import V2CaseSpec


SCORER_REVISION = "content-entailment-v2.1"

# A path is satisfied when every concept group has at least one matching phrase.
# Multiple paths let a report express the same cause in different grounded ways.
TermGroup = tuple[str, ...]
TermPath = tuple[TermGroup, ...]
TermPaths = tuple[TermPath, ...]


CAUSE_PATH_OVERRIDES: dict[str, TermPaths] = {
    "eval2_d006": (
        (("customer",), ("unknown", "unmatched", "not present", "absent")),
    ),
    "eval2_d007": (
        (("reference",), ("stale", "old", "outdated")),
        (("reference",), ("2026-09-14",), ("2026-09-21",), ("refresh", "current reference")),
    ),
    "eval2_d009": (
        (("stale", "old"), ("date", "partition", "2026-09-20")),
        (("2026-09-20",), ("2026-09-21",), ("date", "partition", "processing")),
    ),
    "eval2_d010": (
        (("row", "volume"), ("minimum", "threshold", "below")),
        (("row",), ("at least 3",), ("contains 1", "with 1")),
    ),
}

ABSTENTION_EVIDENCE_PATHS: TermPaths = (
    (("conflict", "cannot distinguish", "uncertain"),),
    (("no diagnostic context", "insufficient evidence", "inconclusive"),),
)

HEALTHY_EVIDENCE_PATHS: TermPaths = (
    (("no failure found",),),
    (("\"status\": \"success\"",), ("\"validation_status\": \"passed\"",)),
)


def _case_cause_paths(case: V2CaseSpec) -> TermPaths:
    override = CAUSE_PATH_OVERRIDES.get(case.run_id)
    if override is not None:
        return override
    return (case.cause_terms,) if case.cause_terms else ()


def matches_any_path(text: str, paths: TermPaths) -> bool:
    lowered = text.lower()
    return any(
        all(any(term.lower() in lowered for term in group) for group in path)
        for path in paths
    )


def cause_identified(case: V2CaseSpec, explanation: str, behavior_correct: bool) -> bool:
    if case.expected_behavior != "diagnose":
        return behavior_correct
    return matches_any_path(explanation, _case_cause_paths(case))


def evidence_sufficient(case: V2CaseSpec, cited_evidence_text: str, evidence_valid: bool) -> bool:
    if not evidence_valid:
        return False
    if case.expected_behavior == "healthy":
        return matches_any_path(cited_evidence_text, HEALTHY_EVIDENCE_PATHS)
    if case.expected_behavior == "abstain":
        return matches_any_path(cited_evidence_text, ABSTENTION_EVIDENCE_PATHS)
    return matches_any_path(cited_evidence_text, _case_cause_paths(case))


def _behavior_correct(case: V2CaseSpec, prediction: str, uncertainty: str | None) -> bool:
    if case.expected_behavior == "diagnose":
        return prediction == case.expected_category and prediction != "unknown"
    if case.expected_behavior == "abstain":
        return prediction == "unknown" and uncertainty == "high"
    return prediction == "unknown" and uncertainty == "low"


def _agent_evidence(agent: dict[str, Any]) -> tuple[bool, str]:
    returned = {
        event["detail"]["reference_id"]: event["detail"]
        for event in agent.get("trace", [])
        if event.get("kind") == "tool_result" and "reference_id" in event.get("detail", {})
    }
    cited = {
        reference.get("reference_id")
        for reference in agent.get("report", {}).get("evidence_references", [])
        if reference.get("reference_id")
    }
    valid = bool(cited) and cited.issubset(returned)
    cited_results = [returned[reference_id] for reference_id in sorted(cited) if reference_id in returned]
    return valid, json.dumps(cited_results, sort_keys=True)


def _baseline_evidence(case: V2CaseSpec, baseline: dict[str, Any]) -> tuple[bool, str]:
    output = baseline.get("output", {})
    if case.expected_behavior == "healthy":
        valid = output.get("source") is None and not output.get("log_excerpt")
    else:
        valid = output.get("source") in {"ingest", "transform", "validate"} and bool(output.get("log_excerpt"))
    return valid, json.dumps(output, sort_keys=True)


def score_record(record: dict[str, Any], case: V2CaseSpec) -> dict[str, Any]:
    """Recompute all outcome booleans from saved raw outputs and traces."""
    baseline = record["baseline"]
    baseline_prediction = baseline["prediction"]
    baseline_behavior = _behavior_correct(case, baseline_prediction, None)
    baseline_valid, baseline_evidence_text = _baseline_evidence(case, baseline)
    baseline.update({
        "category_correct": baseline_prediction == case.expected_category,
        "cause_identified": cause_identified(
            case,
            baseline.get("output", {}).get("finding", ""),
            baseline_behavior,
        ),
        "evidence_valid": baseline_valid,
        "evidence_sufficient": evidence_sufficient(case, baseline_evidence_text, baseline_valid),
        "behavior_correct": baseline_behavior,
    })

    agent = record["agent"]
    agent_prediction = agent["prediction"]
    uncertainty = agent.get("report", {}).get("uncertainty")
    agent_behavior = _behavior_correct(case, agent_prediction, uncertainty)
    agent_valid, agent_evidence_text = _agent_evidence(agent)
    agent.update({
        "category_correct": agent_prediction == case.expected_category,
        "cause_identified": cause_identified(
            case,
            agent.get("report", {}).get("explanation", ""),
            agent_behavior,
        ),
        "evidence_valid": agent_valid,
        "evidence_sufficient": evidence_sufficient(case, agent_evidence_text, agent_valid),
        "behavior_correct": agent_behavior,
    })
    return record
