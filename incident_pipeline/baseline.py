"""A transparent, non-agent symptom baseline over the same evidence tools."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .evidence import LocalArtifactProvider, LogLine, RunEvidenceProvider
from .pipeline import STAGES


@dataclass(frozen=True)
class BaselineResult:
    run_id: str
    finding: str
    source: str | None
    log_excerpt: tuple[LogLine, ...]


def run_baseline(provider: RunEvidenceProvider, run_id: str) -> BaselineResult:
    """Report the first failed check, or the first failed stage if checks never ran."""
    summary = provider.get_run_summary(run_id)
    for check in summary.validation_checks:
        if not check.passed:
            excerpt = provider.read_stage_log(run_id, "validate", max_lines=5)
            return BaselineResult(run_id, f"Validation failed: {check.name}", "validate", excerpt.lines)
    for stage in STAGES:
        if summary.stages[stage].status == "failed":
            excerpt = provider.read_stage_log(run_id, stage, max_lines=5)
            return BaselineResult(run_id, f"Stage failed: {stage}", stage, excerpt.lines)
    return BaselineResult(run_id, "No failure found", None, ())


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the simple failure baseline for a saved run")
    parser.add_argument("run_id")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    result = run_baseline(LocalArtifactProvider(args.artifacts_dir), args.run_id)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
