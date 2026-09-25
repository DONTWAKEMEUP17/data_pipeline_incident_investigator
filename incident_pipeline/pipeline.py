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


class PipelineStageError(RuntimeError):
    """Raised after a failed stage has saved its evidence artifacts."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _validate_input(run_id: str, input_name: str) -> tuple[Path, list[str], list[list[str]]]:
    if (run_id, input_name) not in RUN_INPUTS:
        raise ValueError("run ID and fixture must match a known synthetic run")
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
    return source, columns, rows


def _run_dir(run_id: str, output_dir: Path) -> Path:
    if run_id not in {known_run_id for known_run_id, _ in RUN_INPUTS}:
        raise ValueError(f"unknown synthetic run ID: {run_id!r}")
    run_dir = output_dir / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"ingest artifacts are missing for run {run_id!r}")
    return run_dir


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_log(run_dir: Path, stage: str, lines: list[str]) -> None:
    (run_dir / "logs" / f"{stage}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def ingest_stage(run_id: str, input_name: str, output_dir: Path) -> dict[str, Any]:
    """Create the run snapshot and raw table; safe to retry for the same run directory."""
    source, _, _ = _validate_input(run_id, input_name)

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
    finally:
        connection.close()
    _write_log(run_dir, "ingest", [f"Loaded {input_rows} CSV rows into raw_orders."])
    _write_log(run_dir, "transform", [])
    _write_log(run_dir, "validate", [])
    summary = {"run_id": run_id, "status": "running", "stages": stages}
    _save_json(run_dir / "summary.json", summary)
    _save_json(run_dir / "schemas.json", schemas)
    _save_json(run_dir / "validation.json", validation)
    return summary


def transform_stage(run_id: str, output_dir: Path, *, raise_on_failure: bool = False) -> dict[str, Any]:
    """Validate the raw contract and create orders_daily; save evidence before raising."""
    run_dir = _run_dir(run_id, output_dir)
    summary = _load_json(run_dir / "summary.json")
    schemas = _load_json(run_dir / "schemas.json")
    contract = _load_json(CONTRACT_PATH)
    expected = contract["tables"]["raw_orders"]["columns"]
    observed = schemas["raw_orders"]
    error: str | None = None
    transformed_rows: int | None = None
    connection = duckdb.connect(str(run_dir / "pipeline.duckdb"))
    try:
        connection.execute("DROP TABLE IF EXISTS orders_daily")
        if observed != expected:
            expected_names = [column["name"] for column in expected]
            observed_names = [column["name"] for column in observed]
            error = f"raw_orders schema mismatch: expected {expected_names}; observed {observed_names}"
        else:
            try:
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
            except Exception as exception:
                error = str(exception).splitlines()[0][:500]
    finally:
        connection.close()

    if error is not None:
        schemas.pop("orders_daily", None)
        summary["status"] = "failed"
        summary["stages"]["transform"] = _stage("failed", error=error)
        summary["stages"]["validate"] = _stage("skipped")
        _write_log(run_dir, "transform", [error])
        _write_log(run_dir, "validate", ["Skipped because transform failed."])
    else:
        summary["status"] = "running"
        summary["stages"]["transform"] = _stage("success", transformed_rows)
        _write_log(run_dir, "transform", [f"Created orders_daily with {transformed_rows} rows."])
    _save_json(run_dir / "summary.json", summary)
    _save_json(run_dir / "schemas.json", schemas)
    if error is not None and raise_on_failure:
        raise PipelineStageError("transform", error)
    return summary


def validate_stage(run_id: str, output_dir: Path, *, raise_on_failure: bool = False) -> dict[str, Any]:
    """Run fixed data checks and make the final pipeline status explicit."""
    run_dir = _run_dir(run_id, output_dir)
    summary = _load_json(run_dir / "summary.json")
    if summary["stages"]["transform"]["status"] != "success":
        raise ValueError("validate requires a successful transform stage")
    connection = duckdb.connect(str(run_dir / "pipeline.duckdb"))
    try:
        transformed_rows = connection.execute("SELECT COUNT(*) FROM orders_daily").fetchone()[0]
        null_count = connection.execute(
            "SELECT COUNT(*) FROM orders_daily WHERE order_id IS NULL OR amount IS NULL"
        ).fetchone()[0]
        duplicate_count = connection.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM orders_daily"
        ).fetchone()[0]
    finally:
        connection.close()
    checks = [
        {"name": "row_count_positive", "passed": transformed_rows > 0, "observed": transformed_rows},
        {"name": "required_fields_non_null", "passed": null_count == 0, "observed": null_count},
        {"name": "order_ids_unique", "passed": duplicate_count == 0, "observed": duplicate_count},
    ]
    passed = all(check["passed"] for check in checks)
    validation = {"status": "passed" if passed else "failed", "checks": checks}
    summary["status"] = "success" if passed else "failed"
    summary["stages"]["validate"] = _stage("success" if passed else "failed", transformed_rows)
    _save_json(run_dir / "validation.json", validation)
    _save_json(run_dir / "summary.json", summary)
    _write_log(run_dir, "validate", [f"Completed {len(checks)} checks; status={validation['status']}."])
    if not passed and raise_on_failure:
        failed = [check["name"] for check in checks if not check["passed"]]
        raise PipelineStageError("validate", f"validation failed: {failed}")
    return summary


def generate_run(run_id: str, input_name: str, output_dir: Path) -> dict[str, Any]:
    """Run all stages locally while preserving the original non-raising CLI behavior."""
    ingest_stage(run_id, input_name, output_dir)
    summary = transform_stage(run_id, output_dir)
    if summary["stages"]["transform"]["status"] == "failed":
        return summary
    return validate_stage(run_id, output_dir)


def generate_runs(output_dir: Path) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    return [generate_run(run_id, input_name, output_dir) for run_id, input_name in RUN_INPUTS]
