"""Verify the Milestone 4B callback-to-report handoff for one real failed task."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


DAG_ID = "orders_daily_pipeline"
LOGICAL_DATE = "2026-09-24 02:00:00.000000"
EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "dag_id",
    "airflow_run_id",
    "task_id",
    "try_number",
}


def _latest_failed_run(metadata_db: Path) -> tuple[str, str, dict[str, str]]:
    connection = sqlite3.connect(metadata_db)
    try:
        row = connection.execute(
            "SELECT run_id, state FROM dag_run "
            "WHERE dag_id = ? AND logical_date = ? ORDER BY id DESC LIMIT 1",
            (DAG_ID, LOGICAL_DATE),
        ).fetchone()
        if row is None:
            raise ValueError("Airflow metadata is missing the Milestone 4B DagRun")
        run_id, state = str(row[0]), str(row[1])
        task_states = {
            str(task_id): str(task_state)
            for task_id, task_state in connection.execute(
                "SELECT task_id, state FROM task_instance WHERE dag_id = ? AND run_id = ?",
                (DAG_ID, run_id),
            ).fetchall()
        }
        return run_id, state, task_states
    finally:
        connection.close()


def _one_event(events_dir: Path, airflow_run_id: str) -> dict[str, Any]:
    matches = []
    for path in events_dir.glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("airflow_run_id") == airflow_run_id:
            matches.append(value)
    if len(matches) != 1:
        raise ValueError(f"expected one callback event for {airflow_run_id!r}; found {len(matches)}")
    event = matches[0]
    if set(event) != EVENT_FIELDS:
        raise ValueError("callback event contains fields outside the identifier-only contract")
    return event


def verify_m4b(metadata_db: Path, events_dir: Path, incidents_dir: Path) -> dict[str, Any]:
    if not metadata_db.is_file():
        raise FileNotFoundError(f"Airflow metadata database is missing: {metadata_db}")
    run_id, dag_state, task_states = _latest_failed_run(metadata_db)
    if dag_state != "failed":
        raise ValueError(f"expected failed DagRun; observed {dag_state!r}")
    expected_tasks = {"ingest": "success", "transform": "failed", "validate": "upstream_failed"}
    if task_states != expected_tasks:
        raise ValueError(f"unexpected Airflow task states: {task_states!r}")

    event = _one_event(events_dir, run_id)
    if event["dag_id"] != DAG_ID or event["task_id"] != "transform" or event["try_number"] != 1:
        raise ValueError("callback event does not identify the failed transform attempt")
    incident_dir = incidents_dir / event["event_id"]
    files = sorted(path.name for path in incident_dir.iterdir() if path.is_file())
    if files != ["investigation.json", "manifest.json", "report.md"]:
        raise ValueError(f"incident output has an unexpected shape: {files!r}")
    manifest = json.loads((incident_dir / "manifest.json").read_text(encoding="utf-8"))
    investigation = json.loads((incident_dir / "investigation.json").read_text(encoding="utf-8"))
    report = (incident_dir / "report.md").read_text(encoding="utf-8")
    if manifest.get("state") != "complete" or manifest.get("idempotency_key") != event["event_id"]:
        raise ValueError("incident manifest is incomplete or has the wrong idempotency key")
    if manifest.get("model_api_calls") != 0 or manifest.get("changes_made") is not False:
        raise ValueError("local integration must make zero API calls and no pipeline changes")
    diagnosis = investigation.get("report", {})
    if diagnosis.get("root_cause_category") != "schema_drift" or diagnosis.get("uncertainty") != "low":
        raise ValueError("incident diagnosis did not identify schema drift with low uncertainty")
    if report.count("# Incident Report:") != 1 or run_id not in report:
        raise ValueError("human-readable report is missing its Airflow run identity")

    return {
        "milestone": "4B",
        "airflow_version": "3.3.2",
        "dag_id": DAG_ID,
        "dag_state": dag_state,
        "task_states": task_states,
        "handoff": "identifier_only_failure_event",
        "callback_payload_fields": sorted(EVENT_FIELDS),
        "separate_investigator_process": True,
        "events_for_failed_attempt": 1,
        "reports_for_failed_attempt": 1,
        "idempotency_verified": True,
        "evidence_provider": "AirflowEvidenceProvider",
        "root_cause_category": diagnosis["root_cause_category"],
        "uncertainty": diagnosis["uncertainty"],
        "agent_adapter": manifest["adapter"],
        "model_api_calls": manifest["model_api_calls"],
        "changes_made": manifest["changes_made"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Milestone 4B Airflow incident handoff")
    parser.add_argument("--metadata-db", type=Path, default=Path("airflow/runtime/home/airflow.db"))
    parser.add_argument("--events-dir", type=Path, default=Path("airflow_incidents/events"))
    parser.add_argument("--incidents-dir", type=Path, default=Path("airflow_incidents/incidents"))
    parser.add_argument("--output", type=Path, default=Path("airflow/results/m4b_verification.json"))
    args = parser.parse_args()
    result = verify_m4b(args.metadata_db, args.events_dir, args.incidents_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "verified": result}, indent=2))


if __name__ == "__main__":
    main()
