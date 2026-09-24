"""Generate and sandbox-verify a bounded customer-key repair proposal."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb

from .evaluation_cases_v2 import CASES_V2
from .pipeline import FIXTURE_DIR


MAX_PREVIEW_ROWS = 5
DEVELOPMENT_RUN_IDS = frozenset(case.run_id for case in CASES_V2 if case.split == "development")
CANDIDATES = (
    ("trim", "TRIM(customer_id)"),
    ("upper_trim", "UPPER(TRIM(customer_id))"),
)


@dataclass(frozen=True)
class KeyChangePreview:
    order_id: str
    before: str
    after: str


@dataclass(frozen=True)
class SandboxVerification:
    row_count_before: int
    row_count_after: int
    unmatched_keys_before: int
    unmatched_keys_after: int
    changed_rows: int
    null_keys_before: int
    null_keys_after: int
    empty_keys_after: int
    source_normalization_collisions: int
    reference_duplicate_keys: int
    reference_noncanonical_keys: int
    checks_passed: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RemediationProposal:
    run_id: str
    status: str
    root_cause_category: str
    failure_mechanism: str
    candidate_name: str | None
    candidate_change: str | None
    explanation: str
    assumptions: tuple[str, ...]
    verification: SandboxVerification | None
    change_preview: tuple[KeyChangePreview, ...]
    proposed_human_action: str
    approval_required: bool = True
    sandbox_only: bool = True
    changes_made: bool = False


def _load_reference_ids(path: Path) -> tuple[str, ...]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != ["customer_id"]:
            raise ValueError("reference CSV must contain exactly one customer_id column")
        values = tuple(row["customer_id"] for row in reader)
    if not values or any(not value for value in values):
        raise ValueError("reference CSV must contain non-empty customer IDs")
    return values


def _load_orders(db_path: Path) -> tuple[tuple[str, str | None], ...]:
    if not db_path.is_file():
        raise FileNotFoundError(f"run database is missing: {db_path}")
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "orders_daily" not in tables:
            raise ValueError("run database does not contain orders_daily")
        return tuple(connection.execute(
            "SELECT CAST(order_id AS VARCHAR), CAST(customer_id AS VARCHAR) "
            "FROM orders_daily ORDER BY order_id"
        ).fetchall())
    finally:
        connection.close()


def _blockers(values: dict[str, int]) -> tuple[str, ...]:
    blockers = []
    if values["unmatched_keys_before"] == 0:
        blockers.append("The saved batch has no unmatched customer keys to repair.")
    if values["unmatched_keys_after"] > 0:
        blockers.append("The candidate leaves unmatched customer keys.")
    if values["source_normalization_collisions"] > 0:
        blockers.append("Distinct source keys collapse to the same normalized key.")
    if values["reference_duplicate_keys"] > 0:
        blockers.append("The reference contains duplicate customer keys.")
    if values["reference_noncanonical_keys"] > 0:
        blockers.append("The reference is not canonical under the candidate transformation.")
    if values["row_count_before"] != values["row_count_after"]:
        blockers.append("The candidate changes the order row count.")
    if values["null_keys_before"] != values["null_keys_after"]:
        blockers.append("The candidate changes the number of null customer keys.")
    if values["empty_keys_after"] > 0:
        blockers.append("The candidate creates an empty customer key.")
    return tuple(blockers)


def _verify_candidate(
    orders: tuple[tuple[str, str | None], ...],
    reference_ids: tuple[str, ...],
    expression: str,
) -> tuple[SandboxVerification, tuple[KeyChangePreview, ...]]:
    sandbox = duckdb.connect(":memory:")
    try:
        sandbox.execute("CREATE TABLE source_orders(order_id VARCHAR, customer_id VARCHAR)")
        sandbox.executemany("INSERT INTO source_orders VALUES (?, ?)", orders)
        sandbox.execute("CREATE TABLE customer_reference(customer_id VARCHAR)")
        sandbox.executemany("INSERT INTO customer_reference VALUES (?)", [(value,) for value in reference_ids])
        sandbox.execute(
            f"CREATE TEMP TABLE candidate_orders AS "
            f"SELECT order_id, customer_id AS original_customer_id, {expression} AS customer_id "
            "FROM source_orders"
        )

        row_count_before = sandbox.execute("SELECT COUNT(*) FROM source_orders").fetchone()[0]
        row_count_after = sandbox.execute("SELECT COUNT(*) FROM candidate_orders").fetchone()[0]
        unmatched_before = sandbox.execute(
            "SELECT COUNT(*) FROM source_orders s LEFT JOIN customer_reference r "
            "ON s.customer_id = r.customer_id "
            "WHERE s.customer_id IS NOT NULL AND r.customer_id IS NULL"
        ).fetchone()[0]
        unmatched_after = sandbox.execute(
            "SELECT COUNT(*) FROM candidate_orders s LEFT JOIN customer_reference r "
            "ON s.customer_id = r.customer_id "
            "WHERE s.customer_id IS NOT NULL AND r.customer_id IS NULL"
        ).fetchone()[0]
        changed_rows = sandbox.execute(
            "SELECT COUNT(*) FROM candidate_orders WHERE original_customer_id IS DISTINCT FROM customer_id"
        ).fetchone()[0]
        null_before = sandbox.execute(
            "SELECT COUNT(*) FROM source_orders WHERE customer_id IS NULL"
        ).fetchone()[0]
        null_after = sandbox.execute(
            "SELECT COUNT(*) FROM candidate_orders WHERE customer_id IS NULL"
        ).fetchone()[0]
        empty_after = sandbox.execute(
            "SELECT COUNT(*) FROM candidate_orders WHERE customer_id = ''"
        ).fetchone()[0]
        source_collisions = sandbox.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT customer_id FROM candidate_orders WHERE customer_id IS NOT NULL "
            "GROUP BY customer_id HAVING COUNT(DISTINCT original_customer_id) > 1)"
        ).fetchone()[0]
        reference_duplicates = sandbox.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT customer_id) FROM customer_reference"
        ).fetchone()[0]
        reference_noncanonical = sandbox.execute(
            f"SELECT COUNT(*) FROM customer_reference WHERE customer_id IS DISTINCT FROM {expression}"
        ).fetchone()[0]
        raw_preview = sandbox.execute(
            "SELECT order_id, original_customer_id, customer_id FROM candidate_orders "
            "WHERE original_customer_id IS DISTINCT FROM customer_id ORDER BY order_id LIMIT ?",
            [MAX_PREVIEW_ROWS],
        ).fetchall()
        preview = tuple(KeyChangePreview(*row) for row in raw_preview)
        values = {
            "row_count_before": row_count_before,
            "row_count_after": row_count_after,
            "unmatched_keys_before": unmatched_before,
            "unmatched_keys_after": unmatched_after,
            "changed_rows": changed_rows,
            "null_keys_before": null_before,
            "null_keys_after": null_after,
            "empty_keys_after": empty_after,
            "source_normalization_collisions": source_collisions,
            "reference_duplicate_keys": reference_duplicates,
            "reference_noncanonical_keys": reference_noncanonical,
        }
        blockers = _blockers(values)
        verification = SandboxVerification(**values, checks_passed=not blockers, blockers=blockers)
        return verification, preview
    finally:
        sandbox.close()


def propose_customer_key_remediation(
    run_id: str,
    run_dir: Path,
    reference_csv: Path,
) -> RemediationProposal:
    """Test bounded normalization candidates without modifying saved artifacts."""
    orders = _load_orders(run_dir / "pipeline.duckdb")
    reference_ids = _load_reference_ids(reference_csv)
    attempts = []
    for candidate_name, expression in CANDIDATES:
        verification, preview = _verify_candidate(orders, reference_ids, expression)
        attempts.append((candidate_name, expression, verification, preview))
        if verification.checks_passed:
            return RemediationProposal(
                run_id=run_id,
                status="verified_candidate",
                root_cause_category="join_reference",
                failure_mechanism="customer_key_normalization_mismatch",
                candidate_name=candidate_name,
                candidate_change=f"Apply {expression} to the source customer_id before the reference lookup.",
                explanation=(
                    f"The sandbox found {verification.unmatched_keys_before} unmatched source key and "
                    f"reduced it to {verification.unmatched_keys_after} using {candidate_name}, without "
                    "changing row count or creating normalization collisions."
                ),
                assumptions=(
                    "The supplied customer reference is authoritative for this run.",
                    "Customer IDs are case-insensitive and surrounding whitespace is not meaningful.",
                ),
                verification=verification,
                change_preview=preview,
                proposed_human_action=(
                    "Confirm the customer-ID normalization contract with the source and reference owners. "
                    "If the assumptions are correct, add the verified normalization before the lookup and "
                    "re-run the full pipeline validation."
                ),
            )

    if not attempts:
        raise RuntimeError("no remediation candidates were configured")
    candidate_name, expression, verification, preview = min(
        attempts,
        key=lambda item: (
            item[2].unmatched_keys_after,
            item[2].source_normalization_collisions,
            item[2].changed_rows,
        ),
    )
    return RemediationProposal(
        run_id=run_id,
        status="no_safe_candidate",
        root_cause_category="join_reference",
        failure_mechanism="unresolved_customer_reference_mismatch",
        candidate_name=candidate_name,
        candidate_change=f"Rejected candidate: {expression} on the source customer_id.",
        explanation="The bounded normalization candidates did not pass every sandbox safety check.",
        assumptions=("The supplied customer reference is authoritative for this run.",),
        verification=verification,
        change_preview=preview,
        proposed_human_action="Review the reported blockers and the source/reference key contract before changing data.",
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandbox a customer-key normalization repair proposal")
    parser.add_argument("run_id")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("evaluation/v2/artifacts"))
    parser.add_argument("--reference-csv", type=Path, default=FIXTURE_DIR / "customer_reference.csv")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.run_id not in DEVELOPMENT_RUN_IDS:
        parser.error("run_id must name a v2 development case; heldout remediation is locked")
    output = args.output or Path("evaluation/v2/remediation") / f"{args.run_id}.json"
    proposal = propose_customer_key_remediation(
        args.run_id,
        args.artifacts_dir / args.run_id,
        args.reference_csv,
    )
    encoded = asdict(proposal)
    _write_json(output, encoded)
    print(json.dumps({"output": str(output), "proposal": encoded}, indent=2))


if __name__ == "__main__":
    main()
