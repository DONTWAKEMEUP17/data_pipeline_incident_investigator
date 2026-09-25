"""Build and optionally serve an isolated, deterministic product demo workspace."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .airflow_incidents import process_failure_events
from .airflow_runtime import (
    build_failure_event,
    build_run_descriptor,
    record_task_attempt,
    write_failure_event,
)
from .incident_web import IncidentRepository, create_server
from .pipeline import PipelineStageError, ingest_stage, transform_stage, validate_stage


DEMO_MARKER = ".incident-investigator-demo"
DEMO_RUNS = {
    "healthy": "manual__2026-09-24T10:00:00+00:00",
    "schema_drift": "manual__2026-09-24T11:00:00+00:00",
    "duplicate_id": "manual__2026-09-24T12:00:00+00:00",
}


def _prepare_workspace(workspace: Path) -> dict[str, Path]:
    workspace = Path(workspace)
    marker = workspace / DEMO_MARKER
    if workspace.is_symlink():
        raise ValueError("demo workspace must not be a symbolic link")
    if workspace.exists() and not marker.is_file() and any(workspace.iterdir()):
        raise ValueError("refusing to reuse a non-empty directory that is not a demo workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    marker.write_text("Generated local demo workspace.\n", encoding="utf-8")
    paths = {
        "workspace": workspace,
        "artifacts": workspace / "artifacts",
        "events": workspace / "events",
        "incidents": workspace / "incidents",
        "feedback": workspace / "feedback.sqlite3",
    }
    for name in ("artifacts", "events", "incidents"):
        path = paths[name]
        if path.is_symlink():
            raise ValueError(f"demo {name} directory must not be a symbolic link")
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    return paths


def _event(airflow_run_id: str, task_id: str) -> dict[str, Any]:
    return build_failure_event({
        "dag_id": "orders_daily_pipeline",
        "run_id": airflow_run_id,
        "task_id": task_id,
        "try_number": 1,
    })


def _expect_failure(stage: str, operation: Any) -> None:
    try:
        operation()
    except PipelineStageError as error:
        if error.stage != stage:
            raise ValueError(f"demo expected {stage} failure; observed {error.stage}") from error
    else:
        raise ValueError(f"demo expected {stage} failure, but the stage succeeded")


def create_demo(workspace: Path) -> dict[str, Any]:
    """Create one healthy run and two grounded incidents without requiring Airflow."""
    paths = _prepare_workspace(workspace)
    descriptors = {
        scenario: build_run_descriptor(
            "orders_daily_pipeline",
            airflow_run_id,
            scenario,
            paths["artifacts"],
        )
        for scenario, airflow_run_id in DEMO_RUNS.items()
    }

    healthy = descriptors["healthy"]
    ingest_stage(healthy["pipeline_run_id"], healthy["input_name"], Path(healthy["output_dir"]))
    record_task_attempt(healthy, "ingest", 1, "success")
    transform_stage(healthy["pipeline_run_id"], Path(healthy["output_dir"]), raise_on_failure=True)
    record_task_attempt(healthy, "transform", 1, "success")
    validate_stage(healthy["pipeline_run_id"], Path(healthy["output_dir"]), raise_on_failure=True)
    record_task_attempt(healthy, "validate", 1, "success")

    schema = descriptors["schema_drift"]
    ingest_stage(schema["pipeline_run_id"], schema["input_name"], Path(schema["output_dir"]))
    record_task_attempt(schema, "ingest", 1, "success")
    _expect_failure(
        "transform",
        lambda: transform_stage(schema["pipeline_run_id"], Path(schema["output_dir"]), raise_on_failure=True),
    )
    record_task_attempt(schema, "transform", 1, "failed")
    write_failure_event(_event(schema["airflow_run_id"], "transform"), paths["events"])

    duplicate = descriptors["duplicate_id"]
    ingest_stage(duplicate["pipeline_run_id"], duplicate["input_name"], Path(duplicate["output_dir"]))
    record_task_attempt(duplicate, "ingest", 1, "success")
    transform_stage(
        duplicate["pipeline_run_id"],
        Path(duplicate["output_dir"]),
        raise_on_failure=True,
    )
    record_task_attempt(duplicate, "transform", 1, "success")
    _expect_failure(
        "validate",
        lambda: validate_stage(
            duplicate["pipeline_run_id"],
            Path(duplicate["output_dir"]),
            raise_on_failure=True,
        ),
    )
    record_task_attempt(duplicate, "validate", 1, "failed")
    write_failure_event(_event(duplicate["airflow_run_id"], "validate"), paths["events"])

    worker = process_failure_events(paths["events"], paths["artifacts"], paths["incidents"])
    scan = IncidentRepository(paths["incidents"]).scan()
    incidents = [
        {
            "event_id": incident.event_id,
            "airflow_run_id": incident.manifest["airflow_run_id"],
            "failed_task": incident.manifest["task_id"],
            "root_cause_category": incident.report["root_cause_category"],
        }
        for incident in scan.incidents
    ]
    return {
        "workspace": str(paths["workspace"]),
        "pipeline_runs": {
            "healthy": "success",
            "schema_drift": "failed_at_transform",
            "duplicate_id": "failed_at_validate",
        },
        "incidents": incidents,
        "worker": worker,
        "model_api_calls": worker["model_api_calls"],
        "pipeline_changes_from_investigator": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and serve the isolated local product demo")
    parser.add_argument("--workspace", type=Path, default=Path("demo_workspace"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-serve", action="store_true", help="Build and verify the demo, then exit")
    args = parser.parse_args()
    summary = create_demo(args.workspace)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.no_serve:
        return
    server = create_server(
        args.host,
        args.port,
        args.workspace / "incidents",
        args.workspace / "feedback.sqlite3",
    )
    print(f"Demo ready: http://{args.host}:{server.server_port}")
    print("Press Ctrl+C to stop. Re-running rebuilds the fixed demo runs without accumulating incidents.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
