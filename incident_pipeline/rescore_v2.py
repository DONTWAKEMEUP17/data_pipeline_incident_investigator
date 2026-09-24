"""Recompute benchmark v2 metrics from saved traces without model calls."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from .evaluate_v2 import build_result_v2
from .evaluation_cases_v2 import BENCHMARK_NAME, CASES_V2, EVALUATION_SEED_V2
from .scorer_v2 import SCORER_REVISION, score_record


LABEL_FIELDS = ("split", "family", "variant", "expected_category", "expected_behavior", "expected_cause")


def rescore_document_v2(document: dict[str, Any], *, source_result: str | None = None) -> dict[str, Any]:
    if document.get("benchmark") != BENCHMARK_NAME or document.get("seed") != EVALUATION_SEED_V2:
        raise ValueError("input is not a compatible benchmark v2 result")
    expected = {case.run_id: case for case in CASES_V2}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for original in document.get("cases", []):
        run_id = original.get("run_id")
        if run_id not in expected:
            raise ValueError(f"unknown v2 case {run_id!r}")
        if run_id in seen:
            raise ValueError(f"duplicate v2 case {run_id!r}")
        seen.add(run_id)
        case = expected[run_id]
        for field in LABEL_FIELDS:
            if original.get(field) != getattr(case, field):
                raise ValueError(f"case label mismatch for {run_id}.{field}")
        record = score_record(copy.deepcopy(original), case)
        records.append(record)

    if not records:
        raise ValueError("input result has no cases")
    ordered = sorted(records, key=lambda record: next(
        index for index, case in enumerate(CASES_V2) if case.run_id == record["run_id"]
    ))
    result = build_result_v2(
        document.get("adapter", ""),
        ordered,
        agent_revision=document.get("agent_revision"),
        scorer_revision=SCORER_REVISION,
    )
    result["source_result"] = source_result
    result["previous_scorer_revision"] = document.get("scorer_revision", "legacy-tool-list-v2.0")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Rescore a saved benchmark v2 result without API calls")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = rescore_document_v2(document, source_result=str(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "scorer_revision": result["scorer_revision"],
        "case_count": result["case_count"],
        "overall": result["metrics"]["overall"],
    }, indent=2))


if __name__ == "__main__":
    main()
