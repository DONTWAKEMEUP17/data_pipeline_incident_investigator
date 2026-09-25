"""Local feedback storage kept separate from pipeline and incident artifacts."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


EVENT_ID_PATTERN = re.compile(r"^[a-f0-9]{24}$")
FEEDBACK_RATINGS = frozenset({"useful", "incorrect", "uncertain"})


@dataclass(frozen=True)
class FeedbackRecord:
    event_id: str
    rating: str
    updated_at: str


class FeedbackStore:
    """Store one current review signal per incident in a dedicated SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        if self.database_path.is_symlink():
            raise ValueError("feedback database must not be a symbolic link")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_feedback (
                    event_id TEXT PRIMARY KEY,
                    rating TEXT NOT NULL CHECK (rating IN ('useful', 'incorrect', 'uncertain')),
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _validate(event_id: str, rating: str) -> None:
        if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ValueError("invalid feedback event ID")
        if not isinstance(rating, str) or rating not in FEEDBACK_RATINGS:
            raise ValueError("feedback must be useful, incorrect, or uncertain")

    def save(self, event_id: str, rating: str, *, updated_at: str | None = None) -> FeedbackRecord:
        self._validate(event_id, rating)
        timestamp = updated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incident_feedback (event_id, rating, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    rating = excluded.rating,
                    updated_at = excluded.updated_at
                """,
                (event_id, rating, timestamp),
            )
        return FeedbackRecord(event_id, rating, timestamp)

    def get(self, event_id: str) -> FeedbackRecord | None:
        if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_id, rating, updated_at FROM incident_feedback WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return FeedbackRecord(**dict(row)) if row is not None else None

    def all(self) -> dict[str, FeedbackRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, rating, updated_at FROM incident_feedback ORDER BY event_id"
            ).fetchall()
        return {str(row["event_id"]): FeedbackRecord(**dict(row)) for row in rows}

    def counts(self) -> dict[str, int]:
        counts = {rating: 0 for rating in sorted(FEEDBACK_RATINGS)}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rating, COUNT(*) AS total FROM incident_feedback GROUP BY rating"
            ).fetchall()
        for row in rows:
            counts[str(row["rating"])] = int(row["total"])
        return counts
