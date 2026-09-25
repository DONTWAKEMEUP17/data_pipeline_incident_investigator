"""Small Airflow-facing helpers with no dependency on the Airflow package."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCENARIOS = {
    "healthy": ("healthy_001", "orders_healthy.csv"),
    "schema_drift": ("failed_001", "orders_renamed_column.csv"),
}
TASK_IDS = frozenset({"ingest", "transform", "validate"})
TASK_STATES = frozenset({"success", "failed"})


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

