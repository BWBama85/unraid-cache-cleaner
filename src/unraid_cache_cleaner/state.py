"""Persistent candidate state."""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
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

LOGGER = logging.getLogger(__name__)

# SQLite's default busy timeout is 5s. A slow complete_extraction (an
# executemany over many extraction_outputs rows on a contended disk) can hold the
# write lock longer than that, so a concurrent claim_extraction INSERT would raise
# "database is locked" and surface as a spurious extraction failure (#45). Raise
# the per-connection wait so a claim blocks for the writer to finish instead. This
# is SQLite's own busy-handler (it sleeps and retries acquiring the lock up to the
# timeout), so it needs no application-level retry on top — which would only
# compound the wait and, on the rollback-journal fallback path, risk replaying a
# transaction whose commit failed. Past the timeout the OperationalError simply
# propagates and the caller (Extractor.extract_all) retries the archive next
# cycle, exactly as before — just far less often.
_BUSY_TIMEOUT_SECONDS = 30.0

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

#: SQL ``LIKE`` prefix selecting only the web GUI's reclaim audit rows (written by
#: :class:`~unraid_cache_cleaner.web_actions.ReclaimService` as ``web-reclaim:*``),
#: so the read-only history endpoint never surfaces the cleaner's/extractor's own
#: rows in the shared ``actions`` table.
_WEB_RECLAIM_LIKE = "web-reclaim:%"

#: Hard cap on the history the read-only endpoint returns, so a page load can never
#: pull an unbounded audit table into memory regardless of the requested limit.
_ACTION_HISTORY_MAX = 1000

#: Cap on the per-path audit rows the reconciliation sweep loads for one sibling (#74).
#: A single media path accrues at most a handful of reclaim/reconcile rows, so this
#: bounds a pathological case without limiting any real trail.
_RECONCILE_EVIDENCE_LIMIT = 50


class WebActionHistoryReader:
    """A long-lived, read-only reader for the web-reclaim audit rows (#62).

    Backs the ``/actions`` + ``/api/actions`` history endpoints. Design points, each
    answering a way the naive per-request read went wrong:

    * **One long-lived connection, reused** (opened lazily on first use, guarded by a
      lock). A *per-request* connection on a WAL DB is a write on GET — opening it
      creates the ``-wal``/``-shm`` sidecars and closing the last one checkpoints the
      WAL back into the main file. A single reused connection makes a page load a pure
      ``SELECT``: it never opens/closes per request, so it never checkpoints. The lock
      serializes concurrent request threads (SQLite forbids concurrent use of one
      connection).
    * **``PRAGMA query_only``** hard-refuses any write on the connection, so the reader
      can never mutate the audit table even by mistake.
    * **Never creates the DB**: the connection is opened only once the file exists, so
      a page load on a fresh install returns ``None`` (unavailable) rather than
      creating ``state.sqlite3``.
    * A legacy store with no ``actions`` table, or a corrupt/non-SQLite file, returns
      ``None`` (unavailable) — never a propagated exception or a page 500.

    Only ``web-reclaim:*`` rows are returned (not the cleaner's/extractor's own
    ``actions`` rows), ordered ``occurred_at DESC, id DESC`` and capped. The
    ``ix_actions_occurred_at`` index (created by :meth:`StateStore._initialize`) lets
    that query walk newest-first and stop at the ``LIMIT`` instead of scanning and
    sorting the whole shared table.
    """

    def __init__(self, db_path: Path, *, limit: int = 200) -> None:
        self._db_path = db_path
        self._limit = max(0, min(int(limit), _ACTION_HISTORY_MAX))
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def __call__(self) -> Optional[list[dict]]:
        if self._limit == 0:
            return None
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return None
            try:
                rows = conn.execute(
                    "SELECT path, action, status, size, message, occurred_at "
                    "FROM actions WHERE action LIKE ? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (_WEB_RECLAIM_LIKE, self._limit),
                ).fetchall()
            except sqlite3.DatabaseError:
                # Legacy store (no ``actions`` table → OperationalError) or a corrupt
                # file (DatabaseError, the parent class): unavailable, not empty.
                return None
            return [dict(row) for row in rows]

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if self._conn is not None:
            return self._conn
        if not self._db_path.exists():
            return None  # never create the DB from a page load
        try:
            conn = sqlite3.connect(
                self._db_path, check_same_thread=False, timeout=_BUSY_TIMEOUT_SECONDS
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.DatabaseError:
            return None
        self._conn = conn
        return conn


class StateStore:
    """Tracks candidates across polling cycles."""

    def __init__(
        self,
        db_path: Path,
        *,
        busy_timeout_seconds: float = _BUSY_TIMEOUT_SECONDS,
        check_same_thread: bool = True,
    ) -> None:
        # ``check_same_thread=False`` lets the web action layer (#34 Phase 2) record
        # an audit row from a request worker thread while the store was opened on
        # the main thread. It is only safe because every reclaim serializes all
        # store access behind the ReclaimService lock — SQLite forbids *concurrent*
        # use of one connection, which that lock prevents; it does not forbid use
        # from another thread once the original is quiescent. The default stays
        # True so the single-threaded cleaner/extractor keep the built-in guard.
        self.db_path = db_path
        self._connection = sqlite3.connect(
            self.db_path, timeout=busy_timeout_seconds, check_same_thread=check_same_thread
        )
        self._connection.row_factory = sqlite3.Row
        self._enable_wal()
        self._initialize()

    def _enable_wal(self) -> None:
        """Switch the DB to WAL journaling so readers never block the writer.

        WAL lets a read (``get_protected_extracted_paths``) run concurrently with
        a slow write (``complete_extraction``), cutting the contention that made a
        concurrent claim time out (#45). It does **not** permit two simultaneous
        *writers* — that is what the raised ``busy_timeout`` covers. Setting the
        mode is idempotent and persists in the DB header. The connection already
        carries the raised ``busy_timeout``, so a momentary lock while a concurrent
        process performs the one-time journal→WAL conversion is waited out rather
        than raised. If the underlying mount genuinely rejects WAL (some network
        filesystems do), SQLite silently keeps the prior mode instead of raising,
        so this degrades to the default journaling rather than failing startup;
        the effective mode is logged either way.
        """

        try:
            row = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:  # pragma: no cover - mount-dependent
            LOGGER.warning("Could not enable WAL journaling on %s: %s", self.db_path, exc)
            return
        effective = (row[0] if row else "") or ""
        if effective.lower() != "wal":
            LOGGER.debug(
                "WAL journaling not enabled on %s (effective mode: %s)",
                self.db_path,
                effective,
            )

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
            # Index for the read-only web history query (#62): a plain index on
            # ``occurred_at`` implicitly carries the rowid (= ``id``), so traversing it
            # in reverse yields ``occurred_at DESC, id DESC`` directly — the planner
            # walks newest-first, applies the ``action LIKE 'web-reclaim:%'`` filter,
            # and stops at the ``LIMIT`` instead of scanning and sorting the whole
            # (potentially large) shared actions table on every page load. Created
            # here so an existing store gains it on the next open (additive migration).
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_actions_occurred_at ON actions(occurred_at)"
            )
            # Secondary index on ``path`` for the startup staging-reconciliation sweep
            # (#74): it looks up the recent ``web-reclaim:*`` rows for one media path to
            # tell a committed-purge leftover (→ remove) from a crash-mid-move staging
            # sibling (→ restore). Without it that per-path lookup scans the whole shared
            # actions table; additive, so an existing store gains it on the next open.
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_actions_path ON actions(path)"
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

        Concurrency-safe across processes: the service and a one-shot ``extract`` can
        both open the DB during an upgrade and each compute ``existing`` before the
        other's ``ALTER`` commits, so the loser would hit ``duplicate column name``.
        That is swallowed per column — the column exists either way, so the goal is
        met — rather than aborting startup.
        """

        existing = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name in existing:
                continue
            try:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

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

    def recent_web_reclaim_actions(
        self, path: Path, *, limit: int = _RECONCILE_EVIDENCE_LIMIT
    ) -> list[dict]:
        """Return recent ``web-reclaim:*`` audit rows for ``path`` (newest first).

        Read-only helper for the startup staging-reconciliation sweep (#74), which
        consults the audit trail to disambiguate a missing-original staging sibling: a
        *committed-purge* leftover (an earlier part's unlink already committed, so the
        reclaim is irreversible) is **removed** to complete the delete, while a
        crash-mid-move sibling (nothing committed) is **restored**. Bounded and indexed
        (``ix_actions_path``) so the per-sibling lookup never scans the whole shared
        ``actions`` table. Includes the sweep's own ``web-reclaim:reconcile`` rows (they
        match ``web-reclaim:%``) so evidence a prior sweep already acted on is visible.
        """

        rows = self._connection.execute(
            "SELECT path, action, status, size, message, occurred_at "
            "FROM actions WHERE path = ? AND action LIKE ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (str(path), _WEB_RECLAIM_LIKE, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

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
        a no-op (#41 fix 2). The connection's raised ``busy_timeout`` (#45) makes
        this write wait for a slow concurrent ``complete`` rather than surfacing a
        spurious lock failure.
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

        The superseded archive's recorded outputs are intentionally *left in place*
        here: reclaim happens at claim time, before the replacement has passed its
        integrity test or extracted. Dropping the old outputs now would strip
        protection from the previously extracted media, and a corrupt/aborted
        replacement (which then ``release``s the claim without completing) would
        leave that still-wanted media exposed to the orphan sweep. The old outputs
        are replaced only once the new extraction succeeds — see
        :meth:`complete_extraction`.
        """

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
            # Now that extraction has succeeded, atomically replace this archive's
            # protected outputs: drop the superseded set (a reused path's old files
            # may have unrelated names that the per-path upsert below would not
            # overwrite, leaving now-orphaned media force-protected) and record the
            # new one. Deferring the drop to here — rather than at reclaim time —
            # keeps the previous media protected right up until the replacement is
            # safely on disk.
            self._connection.execute(
                "DELETE FROM extraction_outputs WHERE archive_path = ?",
                (key,),
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

