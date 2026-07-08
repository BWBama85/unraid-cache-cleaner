"""Persistent candidate state."""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from .models import (
    CLAIM_BUSY,
    CLAIM_DONE,
    CLAIM_NEW,
    ActionRecord,
    CandidateRecord,
    ClaimResult,
    FileRecord,
)

# A claim held by a crashed extraction would otherwise block that archive from
# ever being retried. A claim older than this is considered abandoned and may be
# reclaimed. Comfortably longer than any real extraction (unar's own timeout is
# an hour), short enough that a crash recovers within a few poll cycles.
#
# This constant is also the *bound* on the crash-window exposure (#41 fix 3): if a
# process dies after `unar` has written the media but before `complete_extraction`
# records it, the row stays `'claimed'` and the next cycle returns CLAIM_BUSY
# (the media is on disk but unrecorded, hence unprotected) for at most this long.
# Once the TTL lapses the claim is reclaimed, the archive re-extracted, and its
# outputs re-recorded — so eventual correctness holds and the gap is bounded.
# Keep it >= the archive tool's own timeout (default 3600s) so a genuinely slow
# extraction is never mistaken for a crash and reclaimed out from under itself.
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
            # #41 hardening columns, added out-of-band so an operator's existing
            # state DB (created before this change) upgrades in place rather than
            # needing a rebuild. All three are nullable: `size`/`mtime` fingerprint
            # the claimed archive so a *different* archive later written to the same
            # path is re-extracted instead of wrongly skipped; `token` is the
            # per-claim ownership token guarding release/complete against a stale
            # reclaim. Rows written before the upgrade carry NULLs and permanently
            # keep the pre-#41 (path-only, unguarded) behavior; identity/token
            # protection applies to extractions recorded after the upgrade.
            self._ensure_columns(
                "extractions",
                {"size": "INTEGER", "mtime": "REAL", "token": "TEXT"},
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

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """Additively add any missing columns to ``table`` (in-place migration).

        SQLite ``ALTER TABLE ADD COLUMN`` only appends nullable columns, which is
        exactly what the #41 fingerprint/token fields are. Idempotent: a fresh DB
        adds them all once; an already-migrated DB is a no-op. ``table``/column
        names are internal constants, never user input, so the f-string is safe.
        """

        existing = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

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
        size: int,
        mtime: float,
        ttl_seconds: int = CLAIM_TTL_SECONDS,
    ) -> ClaimResult:
        """Atomically claim an archive for extraction (identity- and token-aware).

        ``size``/``mtime`` fingerprint the archive currently on disk. Returns a
        :class:`ClaimResult`:

        - ``CLAIM_NEW`` (with a fresh ownership ``token``) when this caller now
          owns the claim — either the archive is unseen, an equal-path record
          describes a *different* archive (reused path → re-extract, #41 fix 1),
          or a prior claim has gone stale past ``ttl_seconds`` (crash recovery).
        - ``CLAIM_DONE`` when the *same* archive (matching fingerprint) is already
          extracted — skip with no re-invoke.
        - ``CLAIM_BUSY`` when a still-fresh claim is held by another run.

        The ``INSERT OR IGNORE`` + ``rowcount`` check is atomic under SQLite's
        write lock, so a concurrent one-shot ``extract`` and a running ``service``
        cannot both win the same archive. Winning a claim always rotates the
        ``token`` so any earlier owner's in-flight ``release``/``complete`` becomes
        a no-op (#41 fix 2).
        """

        key = str(archive)
        with self._connection:
            token = secrets.token_hex(16)
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO extractions
                    (archive_path, status, claimed_at, size, mtime, token)
                VALUES (?, 'claimed', ?, ?, ?, ?)
                """,
                (key, now, size, mtime, token),
            )
            if cursor.rowcount == 1:
                return ClaimResult(CLAIM_NEW, token)

            row = self._connection.execute(
                "SELECT status, claimed_at, size, mtime FROM extractions WHERE archive_path = ?",
                (key,),
            ).fetchone()
            if row["status"] == "extracted":
                if _identity_matches(row, size, mtime):
                    return ClaimResult(CLAIM_DONE)
                # A genuinely different archive now occupies this path: the old
                # idempotency record must not suppress extracting the new one.
                self._reclaim(key, now, size, mtime, token)
                return ClaimResult(CLAIM_NEW, token)
            if (now - float(row["claimed_at"])) >= ttl_seconds:
                self._reclaim(key, now, size, mtime, token)
                return ClaimResult(CLAIM_NEW, token)
            return ClaimResult(CLAIM_BUSY)

    def _reclaim(self, key: str, now: float, size: int, mtime: float, token: str) -> None:
        """Re-arm an existing row as a fresh ``claimed`` owned by ``token``.

        Used for both a reused-path re-extraction and a stale-TTL recovery. Guarded
        on ``archive_path`` only (not ``status``) because it must overwrite either a
        stale ``claimed`` row or a superseded ``extracted`` one; either way the new
        token revokes the prior owner's write access.

        The superseded archive's recorded outputs are dropped too: when a genuinely
        different archive reuses this path, the previous archive's extracted files
        (which may have unrelated names, so ``complete``'s per-path upsert would not
        overwrite them) must stop being force-protected — otherwise now-orphaned
        media stays undeletable until its window lapses. A stale-``claimed`` recovery
        has no recorded outputs yet, so this is a no-op there.
        """

        self._connection.execute(
            "DELETE FROM extraction_outputs WHERE archive_path = ?",
            (key,),
        )
        self._connection.execute(
            """
            UPDATE extractions
            SET status = 'claimed', claimed_at = ?, size = ?, mtime = ?, token = ?,
                extracted_at = NULL
            WHERE archive_path = ?
            """,
            (now, size, mtime, token, key),
        )

    def complete_extraction(
        self,
        archive: Path,
        outputs: Sequence[Path],
        now: float,
        *,
        token: Optional[str],
    ) -> None:
        """Promote *our* claim to ``extracted`` and record its output files.

        No-op unless the row is still ``claimed`` under the caller's ``token``: if a
        concurrent run reclaimed the archive after our claim went stale, that run —
        not us — owns the outputs, so we must neither promote its claim nor record
        files under it (#41 fix 2).
        """

        key = str(archive)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE extractions SET status = 'extracted', extracted_at = ?
                WHERE archive_path = ? AND status = 'claimed' AND token IS ?
                """,
                (now, key, token),
            )
            if cursor.rowcount == 0:
                return
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

    def release_extraction(self, archive: Path, *, token: Optional[str]) -> None:
        """Drop *our* in-flight claim so a deferred/failed archive retries.

        Only a ``claimed`` row held under the caller's ``token`` is removed: a
        durable ``extracted`` record is never touched, and a claim a concurrent run
        has since reclaimed (different token) is left alone (#41 fix 2).
        """

        with self._connection:
            self._connection.execute(
                "DELETE FROM extractions WHERE archive_path = ? AND status = 'claimed' AND token IS ?",
                (str(archive), token),
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


def _identity_matches(row: sqlite3.Row, size: int, mtime: float) -> bool:
    """Whether a stored extraction row describes the archive now on disk.

    A pre-#41 row (migrated in with NULL ``size``/``mtime``) carries no recorded
    identity, so it cannot be proven stale — treated as a match, which keeps its
    original path-only idempotency permanently. (Identity-based reuse detection
    therefore only ever fires for archives extracted *after* the upgrade; a legacy
    row is never re-extracted on identity grounds.) ``mtime`` is compared exactly:
    an unchanged file returns the same
    ``st_mtime`` every stat, and any real overwrite changes ``mtime`` and/or
    ``size`` — so any difference correctly forces a re-extract.
    """

    stored_size = row["size"]
    stored_mtime = row["mtime"]
    if stored_size is None or stored_mtime is None:
        return True
    return int(stored_size) == size and float(stored_mtime) == mtime


class StateExtractionLedger:
    """Adapts :class:`StateStore` to the extractor's claim/complete/release ledger.

    Kept thin so the extractor stays independent of the persistence layer (it only
    depends on the structural ``ExtractionLedger`` protocol); the concrete store is
    injected here by the CLI/service.
    """

    def __init__(self, store: StateStore, *, claim_ttl_seconds: int = CLAIM_TTL_SECONDS) -> None:
        self._store = store
        self._ttl = claim_ttl_seconds

    def claim(self, archive: Path, now: float, *, size: int, mtime: float) -> ClaimResult:
        return self._store.claim_extraction(
            archive, now, size=size, mtime=mtime, ttl_seconds=self._ttl
        )

    def complete(
        self, archive: Path, outputs: Sequence[Path], now: float, *, token: Optional[str]
    ) -> None:
        self._store.complete_extraction(archive, outputs, now, token=token)

    def release(self, archive: Path, *, token: Optional[str]) -> None:
        self._store.release_extraction(archive, token=token)

