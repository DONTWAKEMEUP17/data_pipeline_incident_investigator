"""A bounded investigation loop with an offline decision adapter.

The local adapter is deliberately deterministic and is not an LLM. It exercises
the model boundary, dynamic tool choice, report validation, and trace format
without credentials or API spend.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Sequence, Union

from .evidence import LocalArtifactProvider, RunEvidenceProvider


MAX_TOOL_CALLS = 5
MAX_MODEL_STEPS = 6
MAX_OBSERVATION_CHARS = 4_000
MAX_REPORT_TEXT_CHARS = 1_000
ALLOWED_TOOLS = frozenset(
    {"get_run_summary", "read_stage_log", "compare_schema", "profile_table", "sample_rows"}
)


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


@dataclass(frozen=True)
class TraceEvent:
    step: int
    kind: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class InvestigationResult:
    report: IncidentReport
    trace: tuple[TraceEvent, ...]
    tool_call_count: int
    model_step_count: int
    model_cost_usd: float | None
    model_api_calls: int
    model_input_tokens: int
    model_output_tokens: int


ModelDecision = Union[ToolRequest, ReportDraft]


class ModelAdapter(Protocol):
    """Boundary for a future configured model provider."""

    name: str
    cost_usd: float | None

    def next_decision(self, run_id: str, observations: Sequence[ToolObservation]) -> ModelDecision: ...


def _observation_for(observations: Sequence[ToolObservation], tool: str, **arguments: Any) -> ToolObservation | None:
    for observation in observations:
        if observation.tool == tool and all(observation.arguments.get(key) == value for key, value in arguments.items()):
            return observation
    return None


def _reference(observation: ToolObservation, claim: str) -> EvidenceReference:
    return EvidenceReference(observation.reference_id, claim)


class DeterministicLearningAdapter:
    """Offline policy used to learn and test the loop before choosing an LLM."""

    name = "deterministic-learning-adapter"
    cost_usd = 0.0

    def next_decision(self, run_id: str, observations: Sequence[ToolObservation]) -> ModelDecision:
        summary = _observation_for(observations, "get_run_summary")
        if summary is None:
            return ToolRequest("get_run_summary", {})
        if summary.error or summary.output is None:
            return self._unknown(summary, "The run summary could not be read.")

        stages = summary.output["stages"]
        checks = summary.output["validation_checks"]
        if summary.output["status"] == "success":
            return ReportDraft(
                RootCauseCategory.UNKNOWN,
                "No failed stage or validation check was observed, so there is no incident cause to diagnose.",
                (_reference(summary, "The run completed successfully."),),
                "Confirm that this is the intended run before investigating another incident.",
                Uncertainty.LOW,
            )

        if stages["transform"]["status"] == "failed":
            schema = _observation_for(observations, "compare_schema", table="raw_orders")
            if schema is None:
                return ToolRequest("compare_schema", {"table": "raw_orders"})
            if schema.error or schema.output is None or schema.output["matches"]:
                return self._unknown(schema, "Transform failed, but the schema evidence does not establish why.")
            profile = _observation_for(observations, "profile_table", table="raw_orders")
            if profile is None:
                return ToolRequest("profile_table", {"table": "raw_orders"})
            expected = [item["name"] for item in schema.output["expected"]]
            observed = [item["name"] for item in schema.output["observed"] or []]
            extra = ""
            references = [_reference(schema, "Expected and observed raw schemas differ.")]
            if profile.output and profile.output.get("duplicate_order_ids", 0) > 0:
                extra = " The raw input also contains duplicate order IDs; this is an additional issue, not the transform blocker."
                references.append(_reference(profile, "The raw table contains duplicate order IDs."))
            return ReportDraft(
                RootCauseCategory.SCHEMA_DRIFT,
                f"The transform was blocked by schema drift: expected columns {expected}, observed {observed}.{extra}",
                tuple(references),
                "Ask the upstream owner whether to restore the contracted schema or approve an explicit mapping, then re-run validation.",
                Uncertainty.LOW,
            )

        failed_checks = [check for check in checks if not check["passed"]]
        if any(check["name"] == "order_ids_unique" for check in failed_checks):
            profile = _observation_for(observations, "profile_table", table="orders_daily")
            if profile is None:
                return ToolRequest("profile_table", {"table": "orders_daily"})
            if profile.error or not profile.output or profile.output["duplicate_order_ids"] is None:
                return self._unknown(profile, "The uniqueness check failed, but the table profile was unavailable.")
            return ReportDraft(
                RootCauseCategory.DATA_QUALITY,
                f"The batch failed its unique-order-ID check; the transformed table has {profile.output['duplicate_order_ids']} duplicate order ID.",
                (
                    _reference(summary, "The order_ids_unique validation check failed."),
                    _reference(profile, "The profile counted duplicate order IDs."),
                ),
                "Inspect the upstream rows for the duplicate ID and decide which record is authoritative before re-running.",
                Uncertainty.LOW,
            )

        if any(check["name"] == "row_count_positive" for check in failed_checks):
            profile = _observation_for(observations, "profile_table", table="raw_orders")
            if profile is None:
                return ToolRequest("profile_table", {"table": "raw_orders"})
            if profile.error or not profile.output:
                return self._unknown(profile, "The row-count check failed, but the raw table profile was unavailable.")
            if profile.output["row_count"] != 0:
                return self._unknown(profile, "The validation result and raw table profile disagree about the row count.")
            return ReportDraft(
                RootCauseCategory.UNKNOWN,
                "The input contains zero rows. The saved evidence cannot distinguish a legitimate empty batch, an upstream outage, or an incorrect filter.",
                (
                    _reference(summary, "The positive-row-count validation failed."),
                    _reference(profile, "The raw table profile contains zero rows."),
                ),
                "Check the expected batch volume and upstream delivery status before assigning a root cause.",
                Uncertainty.HIGH,
            )

        log = _observation_for(observations, "read_stage_log", stage="validate")
        if log is None:
            return ToolRequest("read_stage_log", {"stage": "validate", "max_lines": 5})
        return self._unknown(log, "A validation failed, but the available evidence does not identify its underlying cause.")

    @staticmethod
    def _unknown(observation: ToolObservation, explanation: str) -> ReportDraft:
        return ReportDraft(
            RootCauseCategory.UNKNOWN,
            explanation,
            (_reference(observation, "The requested evidence was unavailable or inconclusive."),),
            "Inspect the cited evidence and collect the missing upstream context before changing the pipeline.",
            Uncertainty.HIGH,
        )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _execute_tool(provider: RunEvidenceProvider, run_id: str, request: ToolRequest) -> dict[str, Any]:
    if request.tool not in ALLOWED_TOOLS:
        raise ValueError(f"unknown tool: {request.tool!r}")
    arguments = dict(request.arguments)
    if "run_id" in arguments:
        raise ValueError("run_id is supplied by the investigation, not by the model")
    method = getattr(provider, request.tool)
    return _jsonable(method(run_id, **arguments))


def _fallback_report(run_id: str, observations: Sequence[ToolObservation], explanation: str) -> IncidentReport:
    evidence = (
        EvidenceReference(observations[-1].reference_id, "The last available tool result is partial or inconclusive."),
    ) if observations else ()
    return IncidentReport(
        run_id,
        RootCauseCategory.UNKNOWN,
        explanation,
        evidence,
        "Review the trace and collect more evidence before changing the pipeline.",
        Uncertainty.HIGH,
    )


def investigate(
    provider: RunEvidenceProvider,
    model: ModelAdapter,
    run_id: str,
    *,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_model_steps: int = MAX_MODEL_STEPS,
) -> InvestigationResult:
    """Run the bounded observe-decide loop without writing artifacts or data."""
    if not 1 <= max_tool_calls <= MAX_TOOL_CALLS:
        raise ValueError(f"max_tool_calls must be from 1 to {MAX_TOOL_CALLS}")
    if not 1 <= max_model_steps <= MAX_MODEL_STEPS:
        raise ValueError(f"max_model_steps must be from 1 to {MAX_MODEL_STEPS}")
    observations: list[ToolObservation] = []
    trace: list[TraceEvent] = []
    report: IncidentReport | None = None
    model_steps = 0

    while model_steps < max_model_steps:
        model_steps += 1
        try:
            decision = model.next_decision(run_id, tuple(observations))
        except Exception as error:
            trace.append(TraceEvent(model_steps, "model_error", {"error": str(error)[:300]}))
            report = _fallback_report(run_id, observations, "The model adapter failed, so the investigator returned a partial report.")
            break
        if isinstance(decision, ReportDraft):
            known_references = {item.reference_id for item in observations}
            cited = {item.reference_id for item in decision.evidence_references}
            if not cited.issubset(known_references):
                trace.append(TraceEvent(model_steps, "validation_error", {"unknown_references": sorted(cited - known_references)}))
                report = _fallback_report(run_id, observations, "The proposed report cited evidence that was not returned by a tool, so the investigator abstained.")
            else:
                report = IncidentReport(run_id, decision.root_cause_category, decision.explanation,
                                        decision.evidence_references, decision.proposed_human_action,
                                        decision.uncertainty)
                trace.append(TraceEvent(model_steps, "final_report", {"root_cause_category": decision.root_cause_category.value}))
            break

        trace.append(TraceEvent(model_steps, "tool_request", {"tool": decision.tool, "arguments": decision.arguments}))
        if model_steps >= max_model_steps:
            report = _fallback_report(run_id, observations, "The final model step requested another tool, leaving no step for a grounded report.")
            trace.append(TraceEvent(model_steps, "budget_exhausted", {"max_model_steps": max_model_steps,
                                                                       "tool_not_executed": decision.tool}))
            break
        if len(observations) >= max_tool_calls:
            report = _fallback_report(run_id, observations, "The tool-call budget was exhausted before the cause could be established.")
            trace.append(TraceEvent(model_steps, "budget_exhausted", {"max_tool_calls": max_tool_calls}))
            break
        reference_id = f"tool-{len(observations) + 1}"
        try:
            output = _execute_tool(provider, run_id, decision)
            encoded = json.dumps(output, sort_keys=True)
            if len(encoded) > MAX_OBSERVATION_CHARS:
                raise ValueError(f"tool output exceeds {MAX_OBSERVATION_CHARS} characters")
            observation = ToolObservation(reference_id, decision.tool, dict(decision.arguments), output, None)
        except Exception as error:
            observation = ToolObservation(reference_id, decision.tool, dict(decision.arguments), None, str(error)[:300])
        observations.append(observation)
        trace.append(TraceEvent(model_steps, "tool_result", {"reference_id": reference_id, "tool": decision.tool,
                                                               "output": observation.output, "error": observation.error}))

    if report is None:
        report = _fallback_report(run_id, observations, "The model-step budget was exhausted before the cause could be established.")
        trace.append(TraceEvent(model_steps, "budget_exhausted", {"max_model_steps": max_model_steps}))
    return InvestigationResult(
        report,
        tuple(trace),
        len(observations),
        model_steps,
        getattr(model, "cost_usd", None),
        int(getattr(model, "api_calls", 0)),
        int(getattr(model, "input_tokens", 0)),
        int(getattr(model, "output_tokens", 0)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Investigate a saved run with the offline learning adapter")
    parser.add_argument("run_id")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--adapter", choices=("local", "openai"), default="local")
    parser.add_argument("--model", help="OpenAI model ID; defaults to OPENAI_MODEL")
    parser.add_argument("--max-api-calls", type=int, default=4)
    parser.add_argument("--max-output-tokens", type=int, default=700)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high"), default="low")
    args = parser.parse_args()
    if args.adapter == "openai":
        from .openai_adapter import OpenAIModelAdapter

        model_name = args.model or os.environ.get("OPENAI_MODEL")
        if not model_name:
            parser.error("--model or OPENAI_MODEL is required with --adapter openai")
        model: ModelAdapter = OpenAIModelAdapter(
            model_name,
            max_api_calls=args.max_api_calls,
            max_output_tokens=args.max_output_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        model_steps = args.max_api_calls
    else:
        model = DeterministicLearningAdapter()
        model_steps = MAX_MODEL_STEPS
    result = investigate(LocalArtifactProvider(args.artifacts_dir), model, args.run_id,
                         max_model_steps=model_steps)
    print(json.dumps(_jsonable(result), indent=2))


if __name__ == "__main__":
    main()
