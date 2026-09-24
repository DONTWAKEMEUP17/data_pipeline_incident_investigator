"""Validate and merge compatible benchmark v2 snapshots without API calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from .evaluate_v2 import build_result_v2
from .evaluation_cases_v2 import BENCHMARK_NAME, CASES_V2, EVALUATION_SEED_V2


LABEL_FIELDS = ("split", "family", "variant", "expected_category", "expected_behavior", "expected_cause")


def merge_results_v2(paths: Iterable[Path]) -> dict[str, Any]:
    source_paths = tuple(Path(path) for path in paths)
    if len(source_paths) < 2:
        raise ValueError("at least two v2 result files are required")

    expected = {case.run_id: case for case in CASES_V2}
    adapter: str | None = None
    agent_revision: str | None = None
    scorer_revision: str | None = None
    revision_initialized = False
    scorer_initialized = False
    records: dict[str, dict[str, Any]] = {}
    for path in source_paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("benchmark") != BENCHMARK_NAME or document.get("seed") != EVALUATION_SEED_V2:
            raise ValueError(f"incompatible v2 benchmark or seed in {path}")
        if adapter is None:
            adapter = document.get("adapter")
        elif document.get("adapter") != adapter:
            raise ValueError(f"incompatible adapter in {path}")
        if not revision_initialized:
            agent_revision = document.get("agent_revision")
            revision_initialized = True
        elif document.get("agent_revision") != agent_revision:
            raise ValueError(f"incompatible agent revision in {path}")
        document_scorer = document.get("scorer_revision", "legacy-tool-list-v2.0")
        if not scorer_initialized:
            scorer_revision = document_scorer
            scorer_initialized = True
        elif document_scorer != scorer_revision:
            raise ValueError(f"incompatible scorer revision in {path}")

        for record in document.get("cases", []):
            run_id = record.get("run_id")
            if run_id not in expected:
                raise ValueError(f"unknown v2 case {run_id!r} in {path}")
            case = expected[run_id]
            for field in LABEL_FIELDS:
                if record.get(field) != getattr(case, field):
                    raise ValueError(f"case label mismatch for {run_id}.{field} in {path}")
            if record.get("required_evidence_tools") != list(case.required_evidence_tools):
                raise ValueError(f"evidence requirement mismatch for {run_id} in {path}")
            if run_id in records:
                raise ValueError(f"duplicate v2 case {run_id!r} across result files")
            records[run_id] = record

    if not adapter:
        raise ValueError("v2 result files do not identify an adapter")
    ordered = [records[case.run_id] for case in CASES_V2 if case.run_id in records]
    if not scorer_revision:
        raise ValueError("v2 result files do not identify a scorer revision")
    merged = build_result_v2(
        adapter,
        ordered,
        agent_revision=agent_revision,
        scorer_revision=scorer_revision,
    )
    merged["source_results"] = [str(path) for path in source_paths]
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge compatible benchmark v2 result snapshots")
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = merge_results_v2(args.inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "case_count": result["case_count"],
        "overall": result["metrics"]["overall"],
    }, indent=2))


if __name__ == "__main__":
    main()
