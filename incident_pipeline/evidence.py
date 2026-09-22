"""Typed, bounded, read-only access to saved synthetic run evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import duckdb

from .pipeline import CONTRACT_PATH, RUN_INPUTS, STAGES


MAX_LOG_LINES = 20
MAX_LOG_LINE_CHARS = 240
MAX_SAMPLE_ROWS = 5
MAX_CELL_CHARS = 120
MAX_COLUMNS = 16
MAX_COLUMN_NAME_CHARS = 80


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    passed: bool
    observed: int


@dataclass(frozen=True)
class StageStatus:
    status: str
    rows: int | None
    error: str | None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: str
    stages: dict[str, StageStatus]
    validation_status: str
    validation_checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True)
class LogLine:
    number: int
    text: str


@dataclass(frozen=True)
class StageLog:
    run_id: str
    stage: str
    lines: tuple[LogLine, ...]
    truncated: bool


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class SchemaComparison:
    run_id: str
    table: str
    expected: tuple[Column, ...]
    observed: tuple[Column, ...] | None
    matches: bool


@dataclass(frozen=True)
class TableProfile:
    run_id: str
    table: str
    row_count: int
    null_counts: dict[str, int]
    duplicate_order_ids: int | None


@dataclass(frozen=True)
class RowSample:
    run_id: str
    table: str
    rows: tuple[dict[str, str | None], ...]
    truncated: bool


class RunEvidenceProvider(Protocol):
    """Interface consumed by a future investigator and today's baseline."""

    def get_run_summary(self, run_id: str) -> RunSummary: ...

    def read_stage_log(self, run_id: str, stage: str, max_lines: int = 10) -> StageLog: ...

    def compare_schema(self, run_id: str, table: str) -> SchemaComparison: ...

    def profile_table(self, run_id: str, table: str) -> TableProfile: ...

    def sample_rows(self, run_id: str, table: str, limit: int = 3) -> RowSample: ...


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded_positive(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    columns = [row[0] for row in connection.execute(f"DESCRIBE {table}").fetchall()]
    if len(columns) > MAX_COLUMNS:
        raise ValueError(f"table exceeds {MAX_COLUMNS} columns")
    if any(len(column) > MAX_COLUMN_NAME_CHARS for column in columns):
        raise ValueError(f"column name exceeds {MAX_COLUMN_NAME_CHARS} characters")
    return columns


class LocalArtifactProvider:
    """Read only known generated runs; never expose general SQL or file paths."""

    def __init__(
        self,
        artifacts_dir: Path = Path("artifacts"),
        *,
        allowed_run_ids: Iterable[str] | None = None,
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.allowed_runs = frozenset(
            allowed_run_ids if allowed_run_ids is not None else (run_id for run_id, _ in RUN_INPUTS)
        )
        if not self.allowed_runs or any(not isinstance(run_id, str) or not run_id for run_id in self.allowed_runs):
            raise ValueError("allowed_run_ids must contain non-empty strings")
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.allowed_tables = frozenset(self.contract["tables"])

    def _run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or run_id not in self.allowed_runs:
            raise ValueError(f"unknown run ID: {run_id!r}")
        run_dir = self.artifacts_dir / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run artifacts are missing: {run_id}")
        return run_dir

    def _table(self, run_id: str, table: str) -> Path:
        run_dir = self._run_dir(run_id)
        if not isinstance(table, str) or table not in self.allowed_tables:
            raise ValueError(f"unknown table: {table!r}")
        observed = json.loads((run_dir / "schemas.json").read_text(encoding="utf-8"))
        if table not in observed:
            raise ValueError(f"table {table!r} was not created in run {run_id!r}")
        return run_dir / "pipeline.duckdb"

    def get_run_summary(self, run_id: str) -> RunSummary:
        run_dir = self._run_dir(run_id)
        raw = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        validation = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
        stages = {
            name: StageStatus(
                status=raw["stages"][name]["status"],
                rows=raw["stages"][name]["rows"],
                error=(
                    _bounded_text(raw["stages"][name]["error"], MAX_LOG_LINE_CHARS)
                    if raw["stages"][name]["error"] is not None else None
                ),
            )
            for name in STAGES
        }
        checks = tuple(
            ValidationCheck(_bounded_text(check["name"], 80), check["passed"], check["observed"])
            for check in validation["checks"][:MAX_COLUMNS]
        )
        return RunSummary(run_id, raw["status"], stages, validation["status"], checks)

    def read_stage_log(self, run_id: str, stage: str, max_lines: int = 10) -> StageLog:
        run_dir = self._run_dir(run_id)
        if not isinstance(stage, str) or stage not in STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        _bounded_positive(max_lines, MAX_LOG_LINES, "max_lines")
        lines = []
        clipped = False
        with (run_dir / "logs" / f"{stage}.log").open(encoding="utf-8") as file:
            for number in range(1, max_lines + 1):
                raw = file.readline(MAX_LOG_LINE_CHARS + 1)
                if not raw:
                    break
                if len(raw) > MAX_LOG_LINE_CHARS:
                    clipped = True
                if not raw.endswith("\n") and len(raw) == MAX_LOG_LINE_CHARS + 1:
                    while True:
                        remainder = file.readline(4096)
                        if not remainder or remainder.endswith("\n"):
                            break
                lines.append(LogLine(number, _bounded_text(raw.rstrip("\r\n"), MAX_LOG_LINE_CHARS)))
            has_more = bool(file.read(1))
        return StageLog(run_id, stage, tuple(lines), clipped or has_more)

    def compare_schema(self, run_id: str, table: str) -> SchemaComparison:
        run_dir = self._run_dir(run_id)
        if not isinstance(table, str) or table not in self.allowed_tables:
            raise ValueError(f"unknown table: {table!r}")
        schemas = json.loads((run_dir / "schemas.json").read_text(encoding="utf-8"))
        expected_raw = self.contract["tables"][table]["columns"]
        observed_raw = schemas.get(table)
        if len(expected_raw) > MAX_COLUMNS or (observed_raw is not None and len(observed_raw) > MAX_COLUMNS):
            raise ValueError(f"schema exceeds {MAX_COLUMNS} columns")
        expected = tuple(Column(_bounded_text(item["name"], 80), _bounded_text(item["type"], 80)) for item in expected_raw)
        observed = (
            tuple(Column(_bounded_text(item["name"], 80), _bounded_text(item["type"], 80)) for item in observed_raw)
            if observed_raw is not None else None
        )
        return SchemaComparison(run_id, table, expected, observed, expected_raw == observed_raw)

    def profile_table(self, run_id: str, table: str) -> TableProfile:
        db_path = self._table(run_id, table)
        connection = duckdb.connect(str(db_path), read_only=True)
        try:
            columns = _table_columns(connection, table)
            row_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            null_counts = {
                column: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {_quote_identifier(column)} IS NULL"
                ).fetchone()[0]
                for column in columns
            }
            duplicate_order_ids = (
                connection.execute(f"SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM {table}").fetchone()[0]
                if "order_id" in columns else None
            )
            return TableProfile(run_id, table, row_count, null_counts, duplicate_order_ids)
        finally:
            connection.close()

    def sample_rows(self, run_id: str, table: str, limit: int = 3) -> RowSample:
        _bounded_positive(limit, MAX_SAMPLE_ROWS, "limit")
        db_path = self._table(run_id, table)
        connection = duckdb.connect(str(db_path), read_only=True)
        try:
            columns = _table_columns(connection, table)
            selected = ", ".join(_quote_identifier(column) for column in columns)
            order = " ORDER BY order_id" if "order_id" in columns else ""
            raw_rows = connection.execute(
                f"SELECT {selected} FROM {table}{order} LIMIT ?", [limit + 1]
            ).fetchall()
            rows = tuple(
                {column: None if value is None else _bounded_text(value, MAX_CELL_CHARS)
                 for column, value in zip(columns, row)}
                for row in raw_rows[:limit]
            )
            return RowSample(run_id, table, rows, len(raw_rows) > limit)
        finally:
            connection.close()
