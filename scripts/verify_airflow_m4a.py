"""Verify the fixed Milestone 4A healthy and failing Airflow runs."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


DAG_ID = "orders_daily_pipeline"
CASES = {
    "healthy": {
        "logical_date": "2026-09-24 00:00:00.000000",
        "dag_state": "success",
        "task_states": {"ingest": "success", "transform": "success", "validate": "success"},
        "pipeline_status": "success",
        "stage_states": {"ingest": "success", "transform": "success", "validate": "success"},
        "validation_status": "passed",
    },
    "schema_drift": {
        "logical_date": "2026-09-24 01:00:00.000000",
        "dag_state": "failed",
        "task_states": {"ingest": "success", "transform": "failed", "validate": "upstream_failed"},
        "pipeline_status": "failed",
        "stage_states": {"ingest": "success", "transform": "failed", "validate": "skipped"},
        "validation_status": "skipped",
    },
    "duplicate_id": {
        "logical_date": "2026-09-24 01:30:00.000000",
        "dag_state": "failed",
        "task_states": {"ingest": "success", "transform": "success", "validate": "failed"},
        "pipeline_status": "failed",
        "stage_states": {"ingest": "success", "transform": "success", "validate": "failed"},
        "validation_status": "failed",
    },
}


def _latest_run(connection: sqlite3.Connection, logical_date: str) -> tuple[str, str]:
    row = connection.execute(
        "SELECT run_id, state FROM dag_run "
        "WHERE dag_id = ? AND logical_date = ? ORDER BY id DESC LIMIT 1",
        (DAG_ID, logical_date),
    ).fetchone()
    if row is None:
        raise ValueError(f"Airflow metadata is missing the run for logical_date {logical_date}")
    return str(row[0]), str(row[1])


def _task_states(connection: sqlite3.Connection, run_id: str) -> dict[str, str]:
    return {
        str(task_id): str(state)
        for task_id, state in connection.execute(
            "SELECT task_id, state FROM task_instance WHERE dag_id = ? AND run_id = ?",
            (DAG_ID, run_id),
        ).fetchall()
    }


def _artifact_for(artifacts_root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    matches = []
    for identity_path in artifacts_root.glob("*/*/airflow_run.json"):
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("airflow_run_id") == run_id:
            matches.append((identity_path.parent, identity))
    if len(matches) != 1:
        raise ValueError(f"expected one artifact directory for Airflow run {run_id!r}; found {len(matches)}")
    run_dir, identity = matches[0]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    validation = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
    return identity, summary, validation


def verify_m4a(metadata_db: Path, artifacts_root: Path) -> dict[str, Any]:
    if not metadata_db.is_file():
        raise FileNotFoundError(f"Airflow metadata database is missing: {metadata_db}")
    connection = sqlite3.connect(metadata_db)
    verified: dict[str, Any] = {}
    try:
        for scenario, expected in CASES.items():
            run_id, dag_state = _latest_run(connection, expected["logical_date"])
            task_states = _task_states(connection, run_id)
            identity, summary, validation = _artifact_for(artifacts_root, run_id)
            stage_states = {name: details["status"] for name, details in summary["stages"].items()}
            observed = {
                "dag_state": dag_state,
                "task_states": task_states,
                "pipeline_status": summary["status"],
                "stage_states": stage_states,
                "validation_status": validation["status"],
            }
            comparable = {key: expected[key] for key in observed}
            if observed != comparable:
                raise ValueError(
                    f"{scenario} verification mismatch: expected {comparable!r}; observed {observed!r}"
                )
            if identity.get("dag_id") != DAG_ID or identity.get("scenario") != scenario:
                raise ValueError(f"{scenario} Airflow identity artifact is inconsistent")
            verified[scenario] = observed
    finally:
        connection.close()
    return {
        "milestone": "4A",
        "airflow_version": "3.3.2",
        "dag_id": DAG_ID,
        "execution_mode": "airflow dags test",
        "agent_connected": False,
        "model_api_calls": 0,
        "cases": verified,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Milestone 4A Airflow metadata and artifacts")
    parser.add_argument("--metadata-db", type=Path, default=Path("airflow/runtime/home/airflow.db"))
    parser.add_argument("--artifacts-dir", type=Path, default=Path("airflow_artifacts"))
    parser.add_argument("--output", type=Path, default=Path("airflow/results/m4a_verification.json"))
    args = parser.parse_args()
    result = verify_m4a(args.metadata_db, args.artifacts_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "verified": result}, indent=2))


if __name__ == "__main__":
    main()
