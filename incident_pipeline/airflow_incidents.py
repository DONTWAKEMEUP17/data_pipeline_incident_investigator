"""Process minimal Airflow failure events outside the Airflow callback process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable

from .agent import MAX_MODEL_STEPS, DeterministicLearningAdapter, ModelAdapter, investigate
from .airflow_runtime import parse_failure_event
from .contracts import jsonable
from .evidence import AirflowEvidenceProvider
from .report import render_markdown


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_write(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def process_failure_events(
    events_dir: Path,
    artifacts_root: Path,
    incidents_dir: Path,
    *,
    model_factory: Callable[[], ModelAdapter] = DeterministicLearningAdapter,
    max_model_steps: int = MAX_MODEL_STEPS,
) -> dict[str, Any]:
    """Process each event once; isolate failures so another event can still complete."""
    events_dir = Path(events_dir)
    incidents_dir = Path(incidents_dir)
    processed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    model_api_calls = 0
    for event_path in sorted(events_dir.glob("*.json")):
        fallback_id = event_path.stem
        try:
            raw_event = json.loads(event_path.read_text(encoding="utf-8"))
            event = parse_failure_event(raw_event)
            incident_dir = incidents_dir / event.event_id
            manifest_path = incident_dir / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("state") != "complete" or manifest.get("event_id") != event.event_id:
                    raise ValueError("existing incident manifest is inconsistent")
                skipped.append(event.event_id)
                continue

            provider = AirflowEvidenceProvider(raw_event, artifacts_root)
            model = model_factory()
            result = investigate(provider, model, event.airflow_run_id, max_model_steps=max_model_steps)
            investigation = jsonable(result)
            model_api_calls += investigation["model_api_calls"]
            report = render_markdown(investigation)
            _json_write(incident_dir / "investigation.json", investigation)
            _atomic_write(incident_dir / "report.md", report)
            manifest = {
                "schema_version": 1,
                "state": "complete",
                "event_id": event.event_id,
                "idempotency_key": event.event_id,
                "dag_id": event.dag_id,
                "airflow_run_id": event.airflow_run_id,
                "task_id": event.task_id,
                "try_number": event.try_number,
                "adapter": getattr(model, "name", type(model).__name__),
                "investigation_file": "investigation.json",
                "report_file": "report.md",
                "root_cause_category": investigation["report"]["root_cause_category"],
                "model_api_calls": investigation["model_api_calls"],
                "changes_made": investigation["report"]["changes_made"],
            }
            _json_write(manifest_path, manifest)
            failure_path = incidents_dir / "failures" / f"{event.event_id}.json"
            failure_path.unlink(missing_ok=True)
            processed.append(event.event_id)
        except Exception as error:
            failed.append(fallback_id)
            _json_write(
                incidents_dir / "failures" / f"{fallback_id}.json",
                {
                    "state": "failed",
                    "event_file": event_path.name,
                    "error_type": type(error).__name__,
                    "error": str(error)[:300],
                },
            )
    return {
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "model_api_calls": model_api_calls,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Process Airflow failure events with the local investigator")
    parser.add_argument("--events-dir", type=Path, default=Path("airflow_incidents/events"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("airflow_artifacts"))
    parser.add_argument("--incidents-dir", type=Path, default=Path("airflow_incidents/incidents"))
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

        def model_factory() -> ModelAdapter:
            return OpenAIModelAdapter(
                model_name,
                max_api_calls=args.max_api_calls,
                max_output_tokens=args.max_output_tokens,
                reasoning_effort=args.reasoning_effort,
            )

        max_model_steps = args.max_api_calls
    else:
        model_factory = DeterministicLearningAdapter
        max_model_steps = MAX_MODEL_STEPS
    result = process_failure_events(
        args.events_dir,
        args.artifacts_dir,
        args.incidents_dir,
        model_factory=model_factory,
        max_model_steps=max_model_steps,
    )
    print(json.dumps(result, indent=2))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
