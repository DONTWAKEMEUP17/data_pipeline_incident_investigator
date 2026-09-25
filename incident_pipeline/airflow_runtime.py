"""Small Airflow-facing helpers with no dependency on the Airflow package."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCENARIOS = {
    "healthy": ("healthy_001", "orders_healthy.csv"),
    "schema_drift": ("failed_001", "orders_renamed_column.csv"),
    "duplicate_id": ("failed_002", "orders_duplicate_id.csv"),
}
TASK_IDS = frozenset({"ingest", "transform", "validate"})
TASK_STATES = frozenset({"success", "failed"})
FAILURE_EVENT_SCHEMA_VERSION = 1
FAILURE_EVENTS_ENV = "INCIDENT_PIPELINE_FAILURE_EVENTS_DIR"
DEFAULT_FAILURE_EVENTS_DIR = Path("/opt/airflow/project/airflow_incidents/events")
EVENT_FIELDS = frozenset({
    "schema_version",
    "event_id",
    "dag_id",
    "airflow_run_id",
    "task_id",
    "try_number",
})


@dataclass(frozen=True)
class FailureEvent:
    schema_version: int
    event_id: str
    dag_id: str
    airflow_run_id: str
    task_id: str
    try_number: int


def _safe_execution_name(airflow_run_id: str) -> str:
    if not airflow_run_id:
        raise ValueError("airflow_run_id must be non-empty")
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", airflow_run_id).strip("_.-")[:80]
    digest = hashlib.sha256(airflow_run_id.encode("utf-8")).hexdigest()[:10]
    return f"{readable or 'run'}-{digest}"


def build_run_descriptor(
    dag_id: str,
    airflow_run_id: str,
    scenario: str,
    artifact_root: Path,
) -> dict[str, str]:
    """Resolve one allowlisted scenario to a collision-resistant artifact directory."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown Airflow scenario: {scenario!r}")
    if not dag_id:
        raise ValueError("dag_id must be non-empty")
    pipeline_run_id, input_name = SCENARIOS[scenario]
    output_dir = Path(artifact_root) / _safe_execution_name(airflow_run_id)
    return {
        "dag_id": dag_id,
        "airflow_run_id": airflow_run_id,
        "scenario": scenario,
        "pipeline_run_id": pipeline_run_id,
        "input_name": input_name,
        "output_dir": str(output_dir),
    }


def record_task_attempt(
    descriptor: dict[str, str],
    task_id: str,
    try_number: int,
    state: str,
) -> Path:
    """Save stable Airflow identity next to pipeline evidence, including failed attempts."""
    if task_id not in TASK_IDS:
        raise ValueError(f"unknown task ID: {task_id!r}")
    if type(try_number) is not int or try_number < 1:
        raise ValueError("try_number must be a positive integer")
    if state not in TASK_STATES:
        raise ValueError(f"unknown task state: {state!r}")
    required = {
        "dag_id",
        "airflow_run_id",
        "scenario",
        "pipeline_run_id",
        "input_name",
        "output_dir",
    }
    if set(descriptor) != required or any(not isinstance(descriptor[key], str) or not descriptor[key]
                                          for key in required):
        raise ValueError("run descriptor has an invalid shape")
    run_dir = Path(descriptor["output_dir"]) / descriptor["pipeline_run_id"]
    if not run_dir.is_dir():
        raise FileNotFoundError("pipeline run directory must exist before recording Airflow identity")
    path = run_dir / "airflow_run.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        value = {
            "dag_id": descriptor["dag_id"],
            "airflow_run_id": descriptor["airflow_run_id"],
            "scenario": descriptor["scenario"],
            "pipeline_run_id": descriptor["pipeline_run_id"],
            "task_attempts": [],
        }
    identity = (task_id, try_number)
    attempts = [
        item for item in value["task_attempts"]
        if (item.get("task_id"), item.get("try_number")) != identity
    ]
    attempts.append({"task_id": task_id, "try_number": try_number, "state": state})
    value["task_attempts"] = sorted(attempts, key=lambda item: (item["task_id"], item["try_number"]))
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def descriptor_for_context(context: dict[str, Any], artifact_root: Path) -> dict[str, str]:
    """Extract only the bounded identifiers needed from an Airflow task context."""
    dag_run = context.get("dag_run")
    conf = getattr(dag_run, "conf", None) or {}
    params = context.get("params") or {}
    scenario = conf.get("scenario", params.get("scenario", "healthy"))
    dag = context.get("dag")
    dag_id = getattr(dag, "dag_id", None)
    airflow_run_id = context.get("run_id")
    if not isinstance(scenario, str) or not isinstance(dag_id, str) or not isinstance(airflow_run_id, str):
        raise ValueError("Airflow context is missing string dag_id, run_id, or scenario")
    return build_run_descriptor(dag_id, airflow_run_id, scenario, artifact_root)


def _context_identifier(context: dict[str, Any], name: str, fallback: Any = None) -> str:
    value = context.get(name, fallback)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Airflow callback context is missing {name}")
    return value


def build_failure_event(context: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal identifier-only event passed to the investigator process."""
    task_instance = context.get("task_instance")
    dag = context.get("dag")
    dag_id = _context_identifier(context, "dag_id", getattr(dag, "dag_id", None))
    airflow_run_id = _context_identifier(context, "run_id", getattr(task_instance, "run_id", None))
    task_id = _context_identifier(context, "task_id", getattr(task_instance, "task_id", None))
    try_number = getattr(task_instance, "try_number", context.get("try_number"))
    if type(try_number) is not int or try_number < 1:
        raise ValueError("Airflow callback context has an invalid try_number")
    if dag_id != "orders_daily_pipeline" or task_id not in TASK_IDS:
        raise ValueError("failure event is outside the allowlisted DAG or tasks")
    identity = f"{dag_id}\0{airflow_run_id}\0{task_id}\0{try_number}"
    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return {
        "schema_version": FAILURE_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "dag_id": dag_id,
        "airflow_run_id": airflow_run_id,
        "task_id": task_id,
        "try_number": try_number,
    }


def parse_failure_event(value: Any) -> FailureEvent:
    """Validate the identifier-only callback contract and its idempotency key."""
    if not isinstance(value, dict) or set(value) != EVENT_FIELDS:
        raise ValueError("failure event has an invalid shape")
    if value.get("schema_version") != FAILURE_EVENT_SCHEMA_VERSION:
        raise ValueError("failure event has an unsupported schema version")
    for name in ("event_id", "dag_id", "airflow_run_id", "task_id"):
        if not isinstance(value.get(name), str) or not value[name]:
            raise ValueError(f"failure event {name} must be a non-empty string")
    try_number = value.get("try_number")
    if type(try_number) is not int or try_number < 1:
        raise ValueError("failure event try_number must be a positive integer")
    if value["dag_id"] != "orders_daily_pipeline":
        raise ValueError("failure event DAG is not allowlisted")
    if value["task_id"] not in TASK_IDS:
        raise ValueError("failure event task is not allowlisted")
    identity = (
        f"{value['dag_id']}\0{value['airflow_run_id']}\0"
        f"{value['task_id']}\0{try_number}"
    )
    expected_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    if value["event_id"] != expected_id:
        raise ValueError("failure event idempotency key does not match its identifiers")
    return FailureEvent(
        schema_version=FAILURE_EVENT_SCHEMA_VERSION,
        event_id=value["event_id"],
        dag_id=value["dag_id"],
        airflow_run_id=value["airflow_run_id"],
        task_id=value["task_id"],
        try_number=try_number,
    )


def write_failure_event(event: dict[str, Any], events_dir: Path) -> Path:
    """Atomically upsert one immutable event; repeated callbacks use the same path."""
    validated = parse_failure_event(event)
    path = Path(events_dir) / f"{validated.event_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(event, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("existing failure event does not match its idempotency key")
        return path
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def emit_failure_event(context: dict[str, Any]) -> None:
    """Airflow callback: enqueue an identifier and never replace the task failure."""
    try:
        event = build_failure_event(context)
        events_dir = Path(os.environ.get(FAILURE_EVENTS_ENV, str(DEFAULT_FAILURE_EVENTS_DIR)))
        path = write_failure_event(event, events_dir)
        print(f"Queued incident event {event['event_id']} at {path}")
    except Exception as error:
        # Callback errors belong to the handoff path. The original task exception remains authoritative.
        print(f"Incident event handoff failed: {type(error).__name__}: {str(error)[:200]}")
