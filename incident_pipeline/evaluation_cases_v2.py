"""Generate benchmark v2 cases that cannot be solved from check names alone."""

from __future__ import annotations

import argparse
import csv
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb

from .pipeline import CONTRACT_PATH, STAGES


BENCHMARK_NAME = "local_incidents_v2"
EVALUATION_SEED_V2 = 20260923
DEFAULT_COLUMNS = ("order_id", "customer_id", "amount", "order_date")
DEFAULT_ROWS = (
    ("O-201", "C-101", "12.50", "2026-09-21"),
    ("O-202", "C-102", "8.25", "2026-09-21"),
    ("O-203", "C-103", "29.00", "2026-09-21"),
)


@dataclass(frozen=True)
class V2CaseSpec:
    run_id: str
    split: str
    family: str
    variant: str
    expected_category: str
    expected_behavior: str
    expected_cause: str
    cause_terms: tuple[tuple[str, ...], ...]
    required_evidence_tools: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...] = DEFAULT_ROWS
    columns: tuple[str, ...] = DEFAULT_COLUMNS
    validation_failure: tuple[str, int] | None = None
    validation_notes: tuple[str, ...] = ()
    transform_error: str | None = None


def _replace_row(index: int, **values: str | None) -> tuple[tuple[str | None, ...], ...]:
    rows = [list(row) for row in DEFAULT_ROWS]
    positions = {name: position for position, name in enumerate(DEFAULT_COLUMNS)}
    for name, value in values.items():
        rows[index][positions[name]] = value
    return tuple(tuple(row) for row in rows)


# Check names are deliberately reused across cause families. The scorer's labels
# stay outside run artifacts, while the investigator receives realistic evidence.
CASES_V2 = (
    V2CaseSpec(
        "eval2_d001", "development", "schema_drift", "renamed monetary column",
        "schema_drift", "diagnose", "renamed_amount_column",
        (("schema", "column"), ("total_amount",), ("amount",)),
        ("compare_schema",),
        columns=("order_id", "customer_id", "total_amount", "order_date"),
    ),
    V2CaseSpec(
        "eval2_d002", "development", "schema_drift", "column order changed",
        "schema_drift", "diagnose", "column_order_changed",
        (("schema", "column"), ("order", "reorder")),
        ("compare_schema",),
        columns=("customer_id", "order_id", "amount", "order_date"),
        rows=tuple((row[1], row[0], row[2], row[3]) for row in DEFAULT_ROWS),
    ),
    V2CaseSpec(
        "eval2_d003", "development", "data_quality", "duplicate order record",
        "data_quality", "diagnose", "duplicate_order_id",
        (("duplicate",), ("order id", "order_id")),
        ("profile_table",),
        rows=DEFAULT_ROWS + (("O-203", "C-104", "7.00", "2026-09-21"),),
    ),
    V2CaseSpec(
        "eval2_d004", "development", "data_quality", "missing required amount",
        "data_quality", "diagnose", "null_amount",
        (("null", "missing"), ("amount",)),
        ("profile_table",),
        rows=_replace_row(1, amount=None),
    ),
    V2CaseSpec(
        "eval2_d005", "development", "data_quality", "non-numeric amount",
        "data_quality", "diagnose", "invalid_amount_format",
        (("amount",), ("numeric", "number", "convert", "decimal")),
        ("read_stage_log", "sample_rows"),
        rows=_replace_row(2, amount="twelve"),
        transform_error="Could not convert raw_orders.amount value 'twelve' to DECIMAL(12,2).",
    ),
    V2CaseSpec(
        "eval2_d006", "development", "join_reference", "unknown source customer key",
        "join_reference", "diagnose", "unknown_customer_key",
        (("customer",), ("unknown", "unmatched", "not present")),
        ("read_stage_log", "sample_rows"),
        rows=_replace_row(2, customer_id="C-999"),
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 compared orders_daily.customer_id with the current customer reference.",
            "One source key was not present in the current reference; inspect row values to identify it.",
        ),
    ),
    V2CaseSpec(
        "eval2_d007", "development", "join_reference", "stale reference snapshot",
        "join_reference", "diagnose", "stale_reference_snapshot",
        (("reference",), ("stale", "old", "outdated")),
        ("read_stage_log",),
        rows=_replace_row(1, customer_id="C-410"),
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 compared customer keys with reference snapshot dated 2026-09-14.",
            "The order batch is dated 2026-09-21; the current reference refresh is missing.",
        ),
    ),
    V2CaseSpec(
        "eval2_d008", "development", "join_reference", "case-sensitive key mismatch",
        "join_reference", "diagnose", "customer_key_case_mismatch",
        (("customer", "key"), ("case", "lowercase", "uppercase")),
        ("read_stage_log", "sample_rows"),
        rows=_replace_row(0, customer_id="c-101"),
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 used a case-sensitive comparison against canonical uppercase customer keys.",
            "One source value did not match; inspect row values before changing the reference.",
        ),
    ),
    V2CaseSpec(
        "eval2_d009", "development", "freshness_volume", "stale input partition",
        "freshness_volume", "diagnose", "stale_input_date",
        (("stale", "old"), ("date", "partition", "2026-09-20")),
        ("read_stage_log", "sample_rows"),
        rows=tuple(row[:3] + ("2026-09-20",) for row in DEFAULT_ROWS),
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 requires the newest input partition to equal processing date 2026-09-21.",
            "The maximum observed order_date was 2026-09-20.",
        ),
    ),
    V2CaseSpec(
        "eval2_d010", "development", "freshness_volume", "volume below contract",
        "freshness_volume", "diagnose", "volume_below_minimum",
        (("row", "volume"), ("minimum", "threshold", "below")),
        ("read_stage_log", "profile_table"),
        rows=DEFAULT_ROWS[:1],
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 requires at least 3 rows for this scheduled weekday batch.",
            "The current batch contains 1 row.",
        ),
    ),
    V2CaseSpec(
        "eval2_d011", "development", "healthy", "ordinary successful batch",
        "unknown", "healthy", "no_incident", (), ("get_run_summary",),
    ),
    V2CaseSpec(
        "eval2_d012", "development", "ambiguous", "conflicting delivery signals",
        "unknown", "abstain", "insufficient_evidence", (),
        ("read_stage_log",),
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 received conflicting source timestamps from two monitors.",
            "Saved evidence cannot distinguish late delivery, clock skew, or a monitor defect.",
        ),
    ),
    V2CaseSpec(
        "eval2_h001", "heldout", "schema_drift", "unexpected currency field",
        "schema_drift", "diagnose", "unexpected_currency_column",
        (("schema", "column"), ("currency", "unexpected", "extra")),
        ("compare_schema",),
        columns=DEFAULT_COLUMNS + ("currency",),
        rows=tuple(row + ("USD",) for row in DEFAULT_ROWS),
    ),
    V2CaseSpec(
        "eval2_h002", "heldout", "data_quality", "null customer key",
        "data_quality", "diagnose", "null_customer_id",
        (("null", "missing"), ("customer",)),
        ("profile_table",),
        rows=_replace_row(2, customer_id=None),
    ),
    V2CaseSpec(
        "eval2_h003", "heldout", "join_reference", "whitespace key mismatch",
        "join_reference", "diagnose", "customer_key_whitespace",
        (("customer", "key"), ("space", "whitespace", "trim")),
        ("read_stage_log", "sample_rows"),
        rows=_replace_row(0, customer_id="C-101 "),
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 used exact matching against canonical customer keys without surrounding spaces.",
            "One source value differed only in its serialized form; inspect row values.",
        ),
    ),
    V2CaseSpec(
        "eval2_h004", "heldout", "freshness_volume", "weekend threshold mismatch",
        "freshness_volume", "diagnose", "wrong_volume_threshold",
        (("threshold",), ("weekend", "schedule", "calendar")),
        ("read_stage_log",),
        rows=DEFAULT_ROWS[:1],
        validation_failure=("gate_17", 1),
        validation_notes=(
            "gate_17 applied the weekday minimum of 3 rows to a scheduled weekend batch.",
            "The weekend contract permits 1 row, so the selected threshold is inconsistent with the schedule.",
        ),
    ),
    V2CaseSpec(
        "eval2_h005", "heldout", "healthy", "successful zero-value order",
        "unknown", "healthy", "no_incident", (), ("get_run_summary",),
        rows=(("O-301", "C-101", "0.00", "2026-09-21"),),
    ),
    V2CaseSpec(
        "eval2_h006", "heldout", "ambiguous", "unexplained generic gate",
        "unknown", "abstain", "insufficient_evidence", (),
        ("read_stage_log",),
        validation_failure=("gate_17", 1),
        validation_notes=("gate_17 failed, but the producing monitor saved no diagnostic context.",),
    ),
)


def case_manifest_v2() -> dict[str, Any]:
    hidden_fields = {
        "rows", "columns", "transform_error", "validation_failure", "validation_notes",
        "cause_terms", "required_evidence_tools",
    }
    return {
        "benchmark": BENCHMARK_NAME,
        "seed": EVALUATION_SEED_V2,
        "labels_are_not_agent_input": True,
        "cases": [
            {key: value for key, value in asdict(case).items() if key not in hidden_fields}
            for case in CASES_V2
        ],
    }


def _schema(connection: duckdb.DuckDBPyConnection, table: str) -> list[dict[str, str]]:
    return [{"name": row[0], "type": row[1]} for row in connection.execute(f"DESCRIBE {table}").fetchall()]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _csv_text(case: V2CaseSpec) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(case.columns)
    writer.writerows(case.rows)
    return output.getvalue()


def _generate_case(case: V2CaseSpec, output_dir: Path) -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    expected_columns = contract["tables"]["raw_orders"]["columns"]
    run_dir = output_dir / case.run_id
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.csv").write_text(_csv_text(case), encoding="utf-8")

    db_path = run_dir / "pipeline.duckdb"
    db_path.unlink(missing_ok=True)
    connection = duckdb.connect(str(db_path))
    schemas: dict[str, list[dict[str, str]]] = {}
    logs = {stage: [] for stage in STAGES}
    stages = {stage: {"status": "skipped", "rows": None, "error": None} for stage in STAGES}
    validation: dict[str, Any] = {"status": "skipped", "checks": []}
    try:
        connection.execute(
            "CREATE TABLE raw_orders AS SELECT * FROM read_csv(?, header = true, all_varchar = true)",
            [str(run_dir / "input.csv")],
        )
        schemas["raw_orders"] = _schema(connection, "raw_orders")
        stages["ingest"] = {"status": "success", "rows": len(case.rows), "error": None}
        logs["ingest"].append(f"Loaded {len(case.rows)} CSV rows into raw_orders.")

        schema_matches = schemas["raw_orders"] == expected_columns
        if not schema_matches or case.transform_error:
            expected_names = [column["name"] for column in expected_columns]
            observed_names = list(case.columns)
            error = case.transform_error or (
                f"raw_orders schema mismatch: expected {expected_names}; observed {observed_names}"
            )
            stages["transform"] = {"status": "failed", "rows": None, "error": error}
            logs["transform"].append(error)
            logs["validate"].append("Skipped because transform failed.")
            status = "failed"
        else:
            connection.execute(
                """CREATE TABLE orders_daily AS
                   SELECT CAST(order_date AS DATE) AS order_date,
                          CAST(order_id AS VARCHAR) AS order_id,
                          CAST(customer_id AS VARCHAR) AS customer_id,
                          CAST(amount AS DECIMAL(12,2)) AS amount
                   FROM raw_orders ORDER BY order_id"""
            )
            schemas["orders_daily"] = _schema(connection, "orders_daily")
            stages["transform"] = {"status": "success", "rows": len(case.rows), "error": None}
            logs["transform"].append(f"Created orders_daily with {len(case.rows)} rows.")

            null_count = sum(row[0] is None or row[1] is None or row[2] is None for row in case.rows)
            order_ids = [row[0] for row in case.rows if row[0] is not None]
            duplicate_count = len(order_ids) - len(set(order_ids))
            checks = [
                {"name": "gate_01", "passed": len(case.rows) > 0, "observed": len(case.rows)},
                {"name": "gate_02", "passed": null_count == 0, "observed": null_count},
                {"name": "gate_03", "passed": duplicate_count == 0, "observed": duplicate_count},
            ]
            if case.validation_failure:
                name, observed = case.validation_failure
                checks.append({"name": name, "passed": False, "observed": observed})
            passed = all(check["passed"] for check in checks)
            validation = {"status": "passed" if passed else "failed", "checks": checks}
            stages["validate"] = {
                "status": "success" if passed else "failed", "rows": len(case.rows), "error": None,
            }
            logs["validate"].append(f"Completed {len(checks)} opaque checks; status={validation['status']}.")
            logs["validate"].extend(case.validation_notes)
            status = "success" if passed else "failed"
    finally:
        connection.close()

    for stage in STAGES:
        (log_dir / f"{stage}.log").write_text("\n".join(logs[stage]) + "\n", encoding="utf-8")
    _write_json(run_dir / "summary.json", {"run_id": case.run_id, "status": status, "stages": stages})
    _write_json(run_dir / "schemas.json", schemas)
    _write_json(run_dir / "validation.json", validation)


def generate_evaluation_cases_v2(
    output_dir: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for case in CASES_V2:
        _generate_case(case, output_dir)
    manifest = case_manifest_v2()
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(manifest_path, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate shortcut-resistant local evaluation cases")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/v2/artifacts"))
    parser.add_argument("--manifest", type=Path, default=Path("evaluation/v2/cases.json"))
    args = parser.parse_args()
    manifest = generate_evaluation_cases_v2(args.output_dir, args.manifest)
    development = sum(case["split"] == "development" for case in manifest["cases"])
    heldout = len(manifest["cases"]) - development
    print(f"Generated {development} development and {heldout} heldout v2 cases.")


if __name__ == "__main__":
    main()
