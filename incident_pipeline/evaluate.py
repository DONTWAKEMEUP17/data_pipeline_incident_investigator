"""Run baseline and agent on identical synthetic cases and save honest metrics."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .agent import DeterministicLearningAdapter, ModelAdapter, investigate
from .baseline import BaselineResult, run_baseline
from .contracts import jsonable
from .evaluation_cases import CASES, EVALUATION_SEED, CaseSpec, generate_evaluation_cases
from .evidence import LocalArtifactProvider


def _baseline_category(result: BaselineResult) -> str:
    """Map the baseline's transparent first symptom to a benchmark category."""
    finding = result.finding.lower()
    log = " ".join(line.text.lower() for line in result.log_excerpt)
    if finding == "no failure found":
        return "unknown"
    if "order_ids_unique" in finding or "required_fields_non_null" in finding:
        return "data_quality"
    if "customer_ids_known" in finding:
        return "join_reference"
    if any(name in finding for name in ("input_date_current", "row_count_minimum", "volume_change_within_range")):
        return "freshness_volume"
    if "stage failed: transform" in finding and "schema mismatch" in log:
        return "schema_drift"
    if "stage failed: transform" in finding and "convert amount" in log:
        return "data_quality"
    return "unknown"


def _behavior_correct(case: CaseSpec, prediction: str, uncertainty: str | None = None) -> bool:
    if case.expected_behavior == "diagnose":
        return prediction == case.expected_category and prediction != "unknown"
    if case.expected_behavior == "abstain":
        return prediction == "unknown" and (uncertainty in (None, "high"))
    return prediction == "unknown" and (uncertainty in (None, "low"))


def _agent_evidence_valid(agent_result: Any) -> bool:
    returned = {
        event.detail["reference_id"]
        for event in agent_result.trace
        if event.kind == "tool_result" and "reference_id" in event.detail
    }
    cited = {reference.reference_id for reference in agent_result.report.evidence_references}
    return bool(cited) and cited.issubset(returned)


def _baseline_evidence_valid(case: CaseSpec, result: BaselineResult) -> bool:
    if case.expected_behavior == "healthy":
        return result.source is None and not result.log_excerpt
    return result.source in {"ingest", "transform", "validate"} and bool(result.log_excerpt)


def _duration_ms(start: float, end: float) -> float:
    return round(max(0.0, (end - start) * 1_000), 3)


def evaluate_cases(
    artifacts_dir: Path,
    cases: Iterable[CaseSpec],
    model_factory: Callable[[], ModelAdapter],
    *,
    adapter_name: str,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    selected = tuple(cases)
    provider = LocalArtifactProvider(artifacts_dir, allowed_run_ids=(case.run_id for case in selected))
    records: list[dict[str, Any]] = []
    for case in selected:
        baseline_start = clock()
        baseline = run_baseline(provider, case.run_id)
        baseline_end = clock()

        model = model_factory()
        agent_start = clock()
        agent = investigate(
            provider,
            model,
            case.run_id,
            max_model_steps=int(getattr(model, "max_api_calls", 6)),
        )
        agent_end = clock()

        baseline_prediction = _baseline_category(baseline)
        agent_prediction = agent.report.root_cause_category.value
        records.append({
            "run_id": case.run_id,
            "split": case.split,
            "family": case.family,
            "variant": case.variant,
            "expected_category": case.expected_category,
            "expected_behavior": case.expected_behavior,
            "baseline": {
                "prediction": baseline_prediction,
                "category_correct": baseline_prediction == case.expected_category,
                "evidence_valid": _baseline_evidence_valid(case, baseline),
                "behavior_correct": _behavior_correct(case, baseline_prediction),
                "tool_calls": 1 + int(baseline.source is not None),
                "latency_ms": _duration_ms(baseline_start, baseline_end),
                "output": asdict(baseline),
            },
            "agent": {
                "prediction": agent_prediction,
                "category_correct": agent_prediction == case.expected_category,
                "evidence_valid": _agent_evidence_valid(agent),
                "behavior_correct": _behavior_correct(case, agent_prediction, agent.report.uncertainty.value),
                "tool_calls": agent.tool_call_count,
                "model_steps": agent.model_step_count,
                "latency_ms": _duration_ms(agent_start, agent_end),
                "model_api_calls": agent.model_api_calls,
                "model_input_tokens": agent.model_input_tokens,
                "model_output_tokens": agent.model_output_tokens,
                "model_cost_usd": agent.model_cost_usd,
                "report": jsonable(agent.report),
                "trace": jsonable(agent.trace),
            },
        })

    return {
        "benchmark": "local_incidents_v1",
        "seed": EVALUATION_SEED,
        "adapter": adapter_name,
        "case_count": len(records),
        "metrics": _metrics(records),
        "error_review": _error_review(records),
        "cases": records,
    }


def _score(rows: list[dict[str, Any]], system: str) -> dict[str, Any]:
    count = len(rows)
    values = [row[system] for row in rows]
    cost_values = [value["model_cost_usd"] for value in values if value.get("model_cost_usd") is not None]
    return {
        "case_count": count,
        "category_accuracy": round(sum(value["category_correct"] for value in values) / count, 4),
        "evidence_validity_rate": round(sum(value["evidence_valid"] for value in values) / count, 4),
        "appropriate_behavior_rate": round(sum(value["behavior_correct"] for value in values) / count, 4),
        "average_tool_calls": round(sum(value["tool_calls"] for value in values) / count, 3),
        "average_latency_ms": round(sum(value["latency_ms"] for value in values) / count, 3),
        "total_model_api_calls": sum(value.get("model_api_calls", 0) for value in values),
        "total_input_tokens": sum(value.get("model_input_tokens", 0) for value in values),
        "total_output_tokens": sum(value.get("model_output_tokens", 0) for value in values),
        "total_model_cost_usd": round(sum(cost_values), 6) if len(cost_values) == count else None,
    }


def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"overall": records}
    for key in ("split", "family"):
        for value in sorted({row[key] for row in records}):
            groups[f"{key}:{value}"] = [row for row in records if row[key] == value]
    return {
        group: {system: _score(rows, system) for system in ("baseline", "agent")}
        for group, rows in groups.items()
    }


def _error_review(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        system: [
            {
                "run_id": row["run_id"],
                "split": row["split"],
                "family": row["family"],
                "expected": row["expected_category"],
                "predicted": row[system]["prediction"],
            }
            for row in records
            if not row[system]["behavior_correct"]
        ]
        for system in ("baseline", "agent")
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate baseline and agent on the same saved cases")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("evaluation/artifacts"))
    parser.add_argument("--manifest", type=Path, default=Path("evaluation/cases.json"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/local_initial.json"))
    parser.add_argument("--split", choices=("all", "development", "heldout"), default="all")
    parser.add_argument("--case", action="append", dest="case_ids",
                        help="Evaluate one named case; repeat to select multiple cases")
    parser.add_argument("--adapter", choices=("local", "openai"), default="local")
    parser.add_argument("--model", help="Required for --adapter openai; may also use OPENAI_MODEL")
    parser.add_argument("--max-api-calls", type=int, default=4)
    parser.add_argument("--max-output-tokens", type=int, default=700)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high"), default="low")
    parser.add_argument("--confirm-paid-evaluation", action="store_true",
                        help="Acknowledge that OpenAI mode may make calls for every selected case")
    args = parser.parse_args()

    generate_evaluation_cases(args.artifacts_dir, args.manifest)
    known_case_ids = {case.run_id for case in CASES}
    if args.case_ids and any(case_id not in known_case_ids for case_id in args.case_ids):
        parser.error("--case must name a case from evaluation/cases.json")
    cases = tuple(
        case for case in CASES
        if (args.split == "all" or case.split == args.split)
        and (not args.case_ids or case.run_id in args.case_ids)
    )
    if args.adapter == "openai":
        if not args.confirm_paid_evaluation:
            parser.error("--confirm-paid-evaluation is required with --adapter openai")
        model_name = args.model or os.environ.get("OPENAI_MODEL")
        if not model_name:
            parser.error("--model or OPENAI_MODEL is required with --adapter openai")
        from .openai_adapter import OpenAIModelAdapter

        def model_factory() -> ModelAdapter:
            return OpenAIModelAdapter(
                model_name,
                max_api_calls=args.max_api_calls,
                max_output_tokens=args.max_output_tokens,
                reasoning_effort=args.reasoning_effort,
            )
        adapter_name = f"openai:{model_name}"
    else:
        model_factory = DeterministicLearningAdapter
        adapter_name = "deterministic-learning-adapter"

    result = evaluate_cases(args.artifacts_dir, cases, model_factory, adapter_name=adapter_name)
    _write_json(args.output, result)
    overall = result["metrics"]["overall"]
    print(json.dumps({"output": str(args.output), "case_count": result["case_count"], "overall": overall}, indent=2))


if __name__ == "__main__":
    main()
