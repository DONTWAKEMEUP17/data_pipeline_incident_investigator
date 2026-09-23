"""Validate and merge compatible saved evaluation snapshots without new API calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from .evaluate import build_result
from .evaluation_cases import CASES, EVALUATION_SEED


BENCHMARK_NAME = "local_incidents_v1"
LABEL_FIELDS = ("split", "family", "variant", "expected_category", "expected_behavior")


def merge_results(paths: Iterable[Path]) -> dict[str, Any]:
    source_paths = tuple(Path(path) for path in paths)
    if len(source_paths) < 2:
        raise ValueError("at least two evaluation result files are required")

    expected = {case.run_id: case for case in CASES}
    adapter: str | None = None
    agent_revision: str | None = None
    revision_initialized = False
    records: dict[str, dict[str, Any]] = {}
    for path in source_paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("benchmark") != BENCHMARK_NAME or document.get("seed") != EVALUATION_SEED:
            raise ValueError(f"incompatible benchmark or seed in {path}")
        if adapter is None:
            adapter = document.get("adapter")
        elif document.get("adapter") != adapter:
            raise ValueError(f"incompatible adapter in {path}")
        if not revision_initialized:
            agent_revision = document.get("agent_revision")
            revision_initialized = True
        elif document.get("agent_revision") != agent_revision:
            raise ValueError(f"incompatible agent revision in {path}")

        for record in document.get("cases", []):
            run_id = record.get("run_id")
            if run_id not in expected:
                raise ValueError(f"unknown case {run_id!r} in {path}")
            case = expected[run_id]
            for field in LABEL_FIELDS:
                if record.get(field) != getattr(case, field):
                    raise ValueError(f"case label mismatch for {run_id}.{field} in {path}")
            if run_id in records:
                raise ValueError(f"duplicate case {run_id!r} across result files")
            records[run_id] = record

    if not adapter:
        raise ValueError("result files do not identify an adapter")
    ordered = [records[case.run_id] for case in CASES if case.run_id in records]
    merged = build_result(adapter, ordered, agent_revision=agent_revision)
    merged["source_results"] = [str(path) for path in source_paths]
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge compatible evaluation result snapshots")
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = merge_results(args.inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "case_count": result["case_count"],
                      "overall": result["metrics"]["overall"]}, indent=2))


if __name__ == "__main__":
    main()
