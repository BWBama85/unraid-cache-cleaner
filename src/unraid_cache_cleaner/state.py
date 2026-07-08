"""Persistent candidate state."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from .models import CLAIM_BUSY, CLAIM_DONE, CLAIM_NEW, ActionRecord, CandidateRecord, FileRecord

# A claim held by a crashed extraction would otherwise block that archive from
# ever being retried. A claim older than this is considered abandoned and may be
# reclaimed. Comfortably longer than any real extraction (unar's own timeout is
# an hour), short enough that a crash recovers within a few poll cycles.
CLAIM_TTL_SECONDS = 7200


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
            # One row per archive that has been claimed/extracted. Replaces the
            # bash tool's flat processed_files.log: `status='claimed'` is an
            # in-flight claim (with a TTL so a crash can't wedge it), `'extracted'`
            # is the durable idempotency record that stops re-extraction.
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS extractions (
                    archive_path TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    claimed_at REAL NOT NULL,
                    extracted_at REAL
                )
                """
            )
            # Files produced by extraction. The deletion planner consults these
            # as first-party protected inputs (Child C) so extracted media is not
            # deleted before Radarr/Sonarr import it, even after the source
            # torrent deregisters. Pruned once older than EXTRACT_PROTECT_SECONDS.
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS extraction_outputs (
                    output_path TEXT PRIMARY KEY,
                    archive_path TEXT NOT NULL,
                    created_at REAL NOT NULL
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

    def claim_extraction(
        self,
        archive: Path,
        now: float,
        *,
        ttl_seconds: int = CLAIM_TTL_SECONDS,
    ) -> str:
        """Atomically claim an archive for extraction.

        Returns ``CLAIM_NEW`` when this caller now owns the claim (proceed to
        extract), ``CLAIM_DONE`` when the archive was already extracted (skip with
        no re-invoke), or ``CLAIM_BUSY`` when a still-fresh claim is held by
        another run. The ``INSERT OR IGNORE`` + ``rowcount`` check is atomic under
        SQLite's write lock, so a concurrent one-shot ``extract`` and a running
        ``service`` cannot both win the same archive.
        """

        key = str(archive)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO extractions (archive_path, status, claimed_at)
                VALUES (?, 'claimed', ?)
                """,
                (key, now),
            )
            if cursor.rowcount == 1:
                return CLAIM_NEW

            row = self._connection.execute(
                "SELECT status, claimed_at FROM extractions WHERE archive_path = ?",
                (key,),
            ).fetchone()
            if row["status"] == "extracted":
                return CLAIM_DONE
            if (now - float(row["claimed_at"])) >= ttl_seconds:
                self._connection.execute(
                    """
                    UPDATE extractions SET claimed_at = ?
                    WHERE archive_path = ? AND status = 'claimed'
                    """,
                    (now, key),
                )
                return CLAIM_NEW
            return CLAIM_BUSY

    def complete_extraction(
        self,
        archive: Path,
        outputs: Sequence[Path],
        now: float,
    ) -> None:
        """Promote a claim to ``extracted`` and record its output files."""

        key = str(archive)
        with self._connection:
            self._connection.execute(
                """
                UPDATE extractions SET status = 'extracted', extracted_at = ?
                WHERE archive_path = ?
                """,
                (now, key),
            )
            if outputs:
                self._connection.executemany(
                    """
                    INSERT INTO extraction_outputs (output_path, archive_path, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(output_path) DO UPDATE SET
                        archive_path = excluded.archive_path,
                        created_at = excluded.created_at
                    """,
                    [(str(path), key, now) for path in outputs],
                )

    def release_extraction(self, archive: Path) -> None:
        """Drop an in-flight claim so a deferred/failed archive retries.

        Only ``claimed`` rows are removed; a durable ``extracted`` record is never
        touched.
        """

        with self._connection:
            self._connection.execute(
                "DELETE FROM extractions WHERE archive_path = ? AND status = 'claimed'",
                (str(archive),),
            )

    def get_protected_extracted_paths(self, now: float, *, protect_seconds: int) -> set[Path]:
        """Return extracted output files still within their protection window."""

        rows = self._connection.execute(
            "SELECT output_path FROM extraction_outputs WHERE (? - created_at) < ?",
            (now, protect_seconds),
        ).fetchall()
        return {Path(row["output_path"]) for row in rows}

    def prune_extraction_outputs(self, now: float, *, protect_seconds: int) -> None:
        """Forget output files past their protection window (normal rules resume)."""

        with self._connection:
            self._connection.execute(
                "DELETE FROM extraction_outputs WHERE (? - created_at) >= ?",
                (now, protect_seconds),
            )


class StateExtractionLedger:
    """Adapts :class:`StateStore` to the extractor's claim/complete/release ledger.

    Kept thin so the extractor stays independent of the persistence layer (it only
    depends on the structural ``ExtractionLedger`` protocol); the concrete store is
    injected here by the CLI/service.
    """

    def __init__(self, store: StateStore, *, claim_ttl_seconds: int = CLAIM_TTL_SECONDS) -> None:
        self._store = store
        self._ttl = claim_ttl_seconds

    def claim(self, archive: Path, now: float) -> str:
        return self._store.claim_extraction(archive, now, ttl_seconds=self._ttl)

    def complete(self, archive: Path, outputs: Sequence[Path], now: float) -> None:
        self._store.complete_extraction(archive, outputs, now)

    def release(self, archive: Path) -> None:
        self._store.release_extraction(archive)

