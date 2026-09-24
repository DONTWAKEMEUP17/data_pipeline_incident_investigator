"""Evaluate benchmark v2 without rewarding check-name shortcuts."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .agent import DeterministicLearningAdapter, ModelAdapter, investigate
from .baseline import run_baseline
from .contracts import jsonable
from .evaluate import _baseline_category
from .evaluation_cases_v2 import (
    BENCHMARK_NAME,
    CASES_V2,
    EVALUATION_SEED_V2,
    V2CaseSpec,
    generate_evaluation_cases_v2,
)
from .evidence import LocalArtifactProvider
from .scorer_v2 import SCORER_REVISION, score_record


def _duration_ms(start: float, end: float) -> float:
    return round(max(0.0, (end - start) * 1_000), 3)


def evaluate_cases_v2(
    artifacts_dir: Path,
    cases: Iterable[V2CaseSpec],
    model_factory: Callable[[], ModelAdapter],
    *,
    adapter_name: str,
    agent_revision: str | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    selected = tuple(cases)
    if not selected:
        raise ValueError("at least one v2 case is required")
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

        record = {
            "run_id": case.run_id,
            "split": case.split,
            "family": case.family,
            "variant": case.variant,
            "expected_category": case.expected_category,
            "expected_behavior": case.expected_behavior,
            "expected_cause": case.expected_cause,
            "required_evidence_tools": list(case.required_evidence_tools),
            "baseline": {
                "prediction": _baseline_category(baseline),
                "tool_calls": 1 + int(baseline.source is not None),
                "latency_ms": _duration_ms(baseline_start, baseline_end),
                "output": asdict(baseline),
            },
            "agent": {
                "prediction": agent.report.root_cause_category.value,
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
        }
        records.append(score_record(record, case))
    return build_result_v2(adapter_name, records, agent_revision=agent_revision)


def _score(rows: list[dict[str, Any]], system: str) -> dict[str, Any]:
    values = [row[system] for row in rows]
    count = len(values)
    cost_values = [value["model_cost_usd"] for value in values if value.get("model_cost_usd") is not None]
    return {
        "case_count": count,
        "category_accuracy": round(sum(value["category_correct"] for value in values) / count, 4),
        "cause_accuracy": round(sum(value["cause_identified"] for value in values) / count, 4),
        "evidence_validity_rate": round(sum(value["evidence_valid"] for value in values) / count, 4),
        "evidence_sufficiency_rate": round(sum(value["evidence_sufficient"] for value in values) / count, 4),
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
                "family": row["family"],
                "expected_category": row["expected_category"],
                "expected_cause": row["expected_cause"],
                "predicted_category": row[system]["prediction"],
                "category_correct": row[system]["category_correct"],
                "cause_identified": row[system]["cause_identified"],
                "evidence_sufficient": row[system]["evidence_sufficient"],
                "behavior_correct": row[system]["behavior_correct"],
            }
            for row in records
            if not (
                row[system]["behavior_correct"]
                and row[system]["cause_identified"]
                and row[system]["evidence_sufficient"]
            )
        ]
        for system in ("baseline", "agent")
    }


def build_result_v2(
    adapter_name: str,
    records: list[dict[str, Any]],
    *,
    agent_revision: str | None = None,
    scorer_revision: str = SCORER_REVISION,
) -> dict[str, Any]:
    return {
        "benchmark": BENCHMARK_NAME,
        "seed": EVALUATION_SEED_V2,
        "adapter": adapter_name,
        "agent_revision": agent_revision,
        "scorer_revision": scorer_revision,
        "case_count": len(records),
        "metrics": _metrics(records),
        "error_review": _error_review(records),
        "cases": records,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate shortcut-resistant benchmark v2")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("evaluation/v2/artifacts"))
    parser.add_argument("--manifest", type=Path, default=Path("evaluation/v2/cases.json"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/v2/results/local_development.json"))
    parser.add_argument("--split", choices=("development", "heldout", "all"), default="development")
    parser.add_argument("--unlock-heldout", action="store_true",
                        help="Required to evaluate heldout or all cases")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--adapter", choices=("local", "openai"), default="local")
    parser.add_argument("--model", help="Required for --adapter openai; may also use OPENAI_MODEL")
    parser.add_argument("--max-api-calls", type=int, default=4)
    parser.add_argument("--max-output-tokens", type=int, default=700)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high"), default="low")
    parser.add_argument("--confirm-paid-evaluation", action="store_true")
    args = parser.parse_args()

    if args.split in {"heldout", "all"} and not args.unlock_heldout:
        parser.error("--unlock-heldout is required for heldout or all")
    generate_evaluation_cases_v2(args.artifacts_dir, args.manifest)
    known = {case.run_id: case for case in CASES_V2}
    if args.case_ids and any(case_id not in known for case_id in args.case_ids):
        parser.error("--case must name a case from evaluation/v2/cases.json")
    if args.case_ids:
        selected_ids = set(args.case_ids)
        cases = tuple(case for case in CASES_V2 if case.run_id in selected_ids)
    else:
        cases = tuple(
            case for case in CASES_V2
            if args.split == "all" or case.split == args.split
        )
    if any(case.split == "heldout" for case in cases) and not args.unlock_heldout:
        parser.error("--unlock-heldout is required when --case selects a heldout case")

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
        agent_revision = OpenAIModelAdapter.prompt_fingerprint
    else:
        model_factory = DeterministicLearningAdapter
        adapter_name = "deterministic-learning-adapter"
        agent_revision = "deterministic-learning-adapter-v1"

    result = evaluate_cases_v2(
        args.artifacts_dir,
        cases,
        model_factory,
        adapter_name=adapter_name,
        agent_revision=agent_revision,
    )
    _write_json(args.output, result)
    print(json.dumps({
        "output": str(args.output),
        "case_count": result["case_count"],
        "overall": result["metrics"]["overall"],
    }, indent=2))


if __name__ == "__main__":
    main()
