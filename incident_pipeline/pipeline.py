"""A tiny, deterministic CSV -> DuckDB batch pipeline.

Run artifacts contain observations, not a diagnosis or benchmark label.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = PROJECT_ROOT / "pipeline_contract.json"
FIXTURE_DIR = PROJECT_ROOT / "fixtures"
RUN_INPUTS = (
    ("healthy_001", "orders_healthy.csv"),
    ("failed_001", "orders_renamed_column.csv"),
    ("failed_002", "orders_duplicate_id.csv"),
    ("ambiguous_001", "orders_empty.csv"),
)
STAGES = ("ingest", "transform", "validate")


def _save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _schema(connection: duckdb.DuckDBPyConnection, table: str) -> list[dict[str, str]]:
    rows = connection.execute(f"DESCRIBE {table}").fetchall()
    return [{"name": row[0], "type": row[1]} for row in rows]


def _stage(status: str, rows: int | None = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "rows": rows, "error": error}


def generate_run(run_id: str, input_name: str, output_dir: Path) -> dict[str, Any]:
    """Save one synthetic run. The only accepted inputs are checked-in fixtures."""
    if (run_id, input_name) not in RUN_INPUTS:
        raise ValueError("run ID and fixture must match a known synthetic run")

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    source = FIXTURE_DIR / input_name
    with source.open(newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        columns = next(reader, None)
        if not columns or len(columns) != len(set(columns)):
            raise ValueError("CSV header must contain unique columns")
        rows = list(reader)
    if any(not row or len(row) != len(columns) for row in rows):
        raise ValueError("CSV rows must have the same number of fields as the header")
    if any(row[0] == "order_id" and row[-1] == "order_date" for row in rows):
        raise ValueError("CSV contains a repeated header; use a separate fixture")

    run_dir = output_dir / run_id
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "input.csv"
    shutil.copyfile(source, snapshot)

    # Recreate only this known generated database, so repeated runs have no stale tables.
    db_path = run_dir / "pipeline.duckdb"
    db_path.unlink(missing_ok=True)
    connection = duckdb.connect(str(db_path))
    stages = {name: _stage("skipped") for name in STAGES}
    logs: dict[str, list[str]] = {name: [] for name in STAGES}
    schemas: dict[str, list[dict[str, str]]] = {}
    validation: dict[str, Any] = {"status": "skipped", "checks": []}
    try:
        connection.execute(
            "CREATE TABLE raw_orders AS SELECT * FROM read_csv(?, header = true, all_varchar = true)",
            [str(snapshot)],
        )
        schemas["raw_orders"] = _schema(connection, "raw_orders")
        input_rows = connection.execute("SELECT COUNT(*) FROM raw_orders").fetchone()[0]
        stages["ingest"] = _stage("success", input_rows)
        logs["ingest"].append(f"Loaded {input_rows} CSV rows into raw_orders.")

        expected = contract["tables"]["raw_orders"]["columns"]
        observed = schemas["raw_orders"]
        if observed != expected:
            expected_names = [column["name"] for column in expected]
            observed_names = [column["name"] for column in observed]
            error = f"raw_orders schema mismatch: expected {expected_names}; observed {observed_names}"
            stages["transform"] = _stage("failed", error=error)
            logs["transform"].append(error)
            logs["validate"].append("Skipped because transform failed.")
            status = "failed"
        else:
            connection.execute(
                """
                CREATE TABLE orders_daily AS
                SELECT CAST(order_date AS DATE) AS order_date,
                       CAST(order_id AS VARCHAR) AS order_id,
                       CAST(customer_id AS VARCHAR) AS customer_id,
                       CAST(amount AS DECIMAL(12, 2)) AS amount
                FROM raw_orders
                ORDER BY order_id
                """
            )
            schemas["orders_daily"] = _schema(connection, "orders_daily")
            transformed_rows = connection.execute("SELECT COUNT(*) FROM orders_daily").fetchone()[0]
            stages["transform"] = _stage("success", transformed_rows)
            logs["transform"].append(f"Created orders_daily with {transformed_rows} rows.")

            null_count = connection.execute(
                "SELECT COUNT(*) FROM orders_daily WHERE order_id IS NULL OR amount IS NULL"
            ).fetchone()[0]
            duplicate_count = connection.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM orders_daily"
            ).fetchone()[0]
            checks = [
                {"name": "row_count_positive", "passed": transformed_rows > 0, "observed": transformed_rows},
                {"name": "required_fields_non_null", "passed": null_count == 0, "observed": null_count},
                {"name": "order_ids_unique", "passed": duplicate_count == 0, "observed": duplicate_count},
            ]
            passed = all(check["passed"] for check in checks)
            validation = {"status": "passed" if passed else "failed", "checks": checks}
            stages["validate"] = _stage("success" if passed else "failed", transformed_rows)
            logs["validate"].append(f"Completed {len(checks)} checks; status={validation['status']}.")
            status = "success" if passed else "failed"
    finally:
        connection.close()
        for stage in STAGES:
            (log_dir / f"{stage}.log").write_text("\n".join(logs[stage]) + "\n", encoding="utf-8")

    summary = {"run_id": run_id, "status": status, "stages": stages}
    _save_json(run_dir / "summary.json", summary)
    _save_json(run_dir / "schemas.json", schemas)
    _save_json(run_dir / "validation.json", validation)
    return summary


def generate_runs(output_dir: Path) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    return [generate_run(run_id, input_name, output_dir) for run_id, input_name in RUN_INPUTS]
