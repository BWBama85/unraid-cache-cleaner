"""Persistent candidate state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import ActionRecord, CandidateRecord, FileRecord


class StateStore:
    """Tracks candidates across polling cycles."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._connection = sqlite3.connect(self.db_path)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                )
                """
            )

    def sync_candidates(self, candidates: dict[Path, FileRecord], now: float) -> None:
        """Replace the live candidate set while preserving first_seen."""

        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO candidates (path, size, mtime, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    last_seen = excluded.last_seen
                """,
                [
                    (str(path), record.size, record.mtime, now, now)
                    for path, record in candidates.items()
                ],
            )

            current_paths = [str(path) for path in candidates]
            if current_paths:
                placeholders = ",".join("?" for _ in current_paths)
                self._connection.execute(
                    f"DELETE FROM candidates WHERE path NOT IN ({placeholders})",
                    current_paths,
                )
            else:
                self._connection.execute("DELETE FROM candidates")

    def get_eligible_candidates(
        self,
        now: float,
        *,
        orphan_grace_seconds: int,
        min_file_age_seconds: int,
    ) -> list[CandidateRecord]:
        """Return candidates eligible for deletion."""

        rows = self._connection.execute(
            """
            SELECT path, size, mtime, first_seen, last_seen
            FROM candidates
            WHERE (? - first_seen) >= ?
              AND (? - mtime) >= ?
            ORDER BY first_seen ASC, path ASC
            """,
            (now, orphan_grace_seconds, now, min_file_age_seconds),
        ).fetchall()
        return [
            CandidateRecord(
                path=Path(row["path"]),
                size=int(row["size"]),
                mtime=float(row["mtime"]),
                first_seen=float(row["first_seen"]),
                last_seen=float(row["last_seen"]),
            )
            for row in rows
        ]

    def remove_candidates(self, paths: list[Path]) -> None:
        """Drop deleted or externally removed candidates from live state."""

        if not paths:
            return
        with self._connection:
            self._connection.executemany(
                "DELETE FROM candidates WHERE path = ?",
                [(str(path),) for path in paths],
            )

    def record_actions(self, actions: list[ActionRecord], now: float) -> None:
        """Persist action history."""

        if not actions:
            return
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO actions (path, action, status, size, message, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (str(action.path), action.action, action.status, action.size, action.message, now)
                    for action in actions
                ],
            )

