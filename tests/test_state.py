"""Extraction ledger (claim / idempotency / output protection) tests."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.extractor import CLAIM_BUSY, CLAIM_DONE, CLAIM_NEW
from unraid_cache_cleaner.models import ActionRecord
from unraid_cache_cleaner.state import StateStore, WebActionHistoryReader


class ExtractionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claim(self, archive: Path, now: float, *, size: int = 1, mtime: float = 1.0, **kw):
        return self.store.claim_extraction(archive, now, size=size, mtime=mtime, **kw)

    def test_claim_is_new_then_done_after_complete(self) -> None:
        archive = Path("/data/rel/movie.rar")

        claim = self._claim(archive, 1000.0, size=10, mtime=100.0)
        self.assertEqual(claim.decision, CLAIM_NEW)
        self.assertIsNotNone(claim.token)
        # A second claim while still 'claimed' is busy (owned by the first caller).
        self.assertEqual(self._claim(archive, 1000.0, size=10, mtime=100.0).decision, CLAIM_BUSY)

        self.store.complete_extraction(
            archive, [Path("/data/rel/movie.mkv")], 1001.0, token=claim.token
        )
        # The same archive (matching fingerprint) is now done.
        self.assertEqual(self._claim(archive, 1002.0, size=10, mtime=100.0).decision, CLAIM_DONE)

    def test_release_lets_a_deferred_archive_retry(self) -> None:
        archive = Path("/data/rel/movie.rar")

        claim = self._claim(archive, 1000.0)
        self.assertEqual(claim.decision, CLAIM_NEW)
        self.store.release_extraction(archive, token=claim.token)
        # After release the claim is gone, so a later cycle wins a fresh claim.
        self.assertEqual(self._claim(archive, 1001.0).decision, CLAIM_NEW)

    def test_release_never_drops_a_completed_record(self) -> None:
        archive = Path("/data/rel/movie.rar")
        claim = self._claim(archive, 1000.0)
        self.store.complete_extraction(archive, [], 1001.0, token=claim.token)

        self.store.release_extraction(archive, token=claim.token)  # no-op on 'extracted'

        self.assertEqual(self._claim(archive, 1002.0).decision, CLAIM_DONE)

    def test_stale_claim_is_reclaimable(self) -> None:
        archive = Path("/data/rel/movie.rar")
        self.assertEqual(self._claim(archive, 1000.0, ttl_seconds=100).decision, CLAIM_NEW)

        # Within the TTL: still busy. Past the TTL: reclaimable (crash recovery).
        self.assertEqual(self._claim(archive, 1050.0, ttl_seconds=100).decision, CLAIM_BUSY)
        self.assertEqual(self._claim(archive, 1200.0, ttl_seconds=100).decision, CLAIM_NEW)

    def test_reused_path_with_different_identity_reextracts(self) -> None:
        # #41 fix 1: a genuinely different archive written to a path that once held
        # an extracted archive must re-extract, not return the stale CLAIM_DONE.
        archive = Path("/data/rel/movie.rar")
        first = self._claim(archive, 1000.0, size=10, mtime=100.0)
        self.store.complete_extraction(archive, [Path("/data/rel/movie.mkv")], 1000.0, token=first.token)

        # Same identity → still idempotently done.
        self.assertEqual(self._claim(archive, 1100.0, size=10, mtime=100.0).decision, CLAIM_DONE)
        # Changed size → a new archive → re-extract.
        changed_size = self._claim(archive, 1200.0, size=20, mtime=100.0)
        self.assertEqual(changed_size.decision, CLAIM_NEW)
        self.store.complete_extraction(archive, [Path("/data/rel/movie.mkv")], 1200.0, token=changed_size.token)
        # Changed mtime alone (same size) → also a new archive.
        self.assertEqual(self._claim(archive, 1300.0, size=20, mtime=200.0).decision, CLAIM_NEW)

    def test_reused_path_replaces_protected_outputs_on_completion(self) -> None:
        # #41 fix 1 cleanup: when a different archive reuses a path and its extraction
        # *succeeds*, the previous archive's recorded outputs (which may have
        # unrelated names) must stop being force-protected — otherwise now-orphaned
        # media stays undeletable.
        path = Path("/data/rel/movie.rar")
        old = self._claim(path, 1000.0, size=10, mtime=100.0)
        self.store.complete_extraction(path, [Path("/data/rel/old.mkv")], 1000.0, token=old.token)
        self.assertEqual(
            self.store.get_protected_extracted_paths(1000.0, protect_seconds=10**9),
            {Path("/data/rel/old.mkv")},
        )

        new = self._claim(path, 2000.0, size=20, mtime=200.0)  # different archive, same path
        self.assertEqual(new.decision, CLAIM_NEW)
        self.store.complete_extraction(path, [Path("/data/rel/new.mkv")], 2000.0, token=new.token)

        # Only the new archive's output is protected; old.mkv is no longer force-held.
        self.assertEqual(
            self.store.get_protected_extracted_paths(2000.0, protect_seconds=10**9),
            {Path("/data/rel/new.mkv")},
        )

    def test_reclaim_keeps_old_outputs_protected_until_replacement_completes(self) -> None:
        # #41 fix 1, corrected: a reused path whose replacement is corrupt/incomplete
        # (claim then release, never complete) must NOT strip protection from the
        # previously extracted media — it is still the best copy on disk.
        path = Path("/data/rel/movie.rar")
        old = self._claim(path, 1000.0, size=10, mtime=100.0)
        self.store.complete_extraction(path, [Path("/data/rel/old.mkv")], 1000.0, token=old.token)

        # A different archive reuses the path (identity differs) → reclaim, but its
        # integrity test / extraction fails, so the claim is released uncompleted.
        replacement = self._claim(path, 2000.0, size=20, mtime=200.0)
        self.assertEqual(replacement.decision, CLAIM_NEW)
        self.store.release_extraction(path, token=replacement.token)

        # The old media stays protected — not exposed to the orphan sweep.
        self.assertEqual(
            self.store.get_protected_extracted_paths(2000.0, protect_seconds=10**9),
            {Path("/data/rel/old.mkv")},
        )

    def test_ensure_columns_tolerates_a_racing_duplicate_add(self) -> None:
        # Two processes upgrading a pre-#41 DB can both snapshot the schema before
        # either ALTER commits; the loser's ALTER then raises "duplicate column
        # name". _ensure_columns must swallow that (the column exists either way)
        # rather than crash startup, so a re-run of the migration is a no-op.
        db = Path(self._tmp.name) / "race.sqlite3"
        legacy = sqlite3.connect(db)
        legacy.execute(
            """
            CREATE TABLE extractions (
                archive_path TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                extracted_at REAL
            )
            """
        )
        legacy.commit()
        legacy.close()

        store = StateStore(db)  # migrates once
        # The DB genuinely rejects a duplicate ALTER — the precondition the guard
        # relies on (and a guard against SQLite changing the error text).
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            store._connection.execute("ALTER TABLE extractions ADD COLUMN size INTEGER")
        self.assertIn("duplicate column name", str(ctx.exception).lower())
        # Re-running the whole migration (as a second process would) does not raise.
        store._ensure_columns("extractions", {"size": "INTEGER", "mtime": "REAL", "token": "TEXT"})
        columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(extractions)")}
        self.assertTrue({"size", "mtime", "token"} <= columns)

    def test_reclaim_revokes_the_previous_owners_token(self) -> None:
        # #41 fix 2: after A's claim goes stale and B reclaims, A's late
        # release/complete must not touch B's live claim.
        archive = Path("/data/rel/movie.rar")
        owner_a = self._claim(archive, 1000.0, ttl_seconds=100)
        self.assertEqual(owner_a.decision, CLAIM_NEW)

        owner_b = self._claim(archive, 1200.0, ttl_seconds=100)  # reclaim past TTL
        self.assertEqual(owner_b.decision, CLAIM_NEW)
        self.assertNotEqual(owner_a.token, owner_b.token)

        # A's stale release cannot delete B's claim.
        self.store.release_extraction(archive, token=owner_a.token)
        self.assertEqual(self._claim(archive, 1200.0, ttl_seconds=100).decision, CLAIM_BUSY)

        # A's stale complete cannot promote B's claim nor record ghost outputs.
        self.store.complete_extraction(
            archive, [Path("/data/rel/ghost.mkv")], 1200.0, token=owner_a.token
        )
        self.assertEqual(
            self.store.get_protected_extracted_paths(1200.0, protect_seconds=10**9), set()
        )
        self.assertEqual(self._claim(archive, 1200.0, ttl_seconds=100).decision, CLAIM_BUSY)

        # B still owns the claim and can complete it normally.
        self.store.complete_extraction(
            archive, [Path("/data/rel/real.mkv")], 1200.0, token=owner_b.token
        )
        self.assertEqual(
            self.store.get_protected_extracted_paths(1200.0, protect_seconds=10**9),
            {Path("/data/rel/real.mkv")},
        )

    def test_crash_window_is_bounded_and_recovers_after_ttl(self) -> None:
        # #41 fix 3: a crash after the media is written but before complete() leaves
        # the row 'claimed'. Within the TTL the media is unrecorded/unprotected (the
        # bounded exposure); past the TTL the claim is reclaimed and re-extraction
        # can re-record and protect it.
        archive = Path("/data/rel/movie.rar")
        crashed = self._claim(archive, 1000.0, ttl_seconds=100)
        self.assertEqual(crashed.decision, CLAIM_NEW)
        # (process dies before complete() — no outputs recorded)

        self.assertEqual(self._claim(archive, 1050.0, ttl_seconds=100).decision, CLAIM_BUSY)
        self.assertEqual(
            self.store.get_protected_extracted_paths(1050.0, protect_seconds=10**9), set()
        )

        recovered = self._claim(archive, 1200.0, ttl_seconds=100)
        self.assertEqual(recovered.decision, CLAIM_NEW)
        self.store.complete_extraction(
            archive, [Path("/data/rel/movie.mkv")], 1200.0, token=recovered.token
        )
        self.assertEqual(
            self.store.get_protected_extracted_paths(1200.0, protect_seconds=10**9),
            {Path("/data/rel/movie.mkv")},
        )

    def test_migrates_legacy_extractions_schema_in_place(self) -> None:
        # An operator DB created before #41 has no size/mtime/token columns. Opening
        # a StateStore on it must add them (ALTER TABLE) without a rebuild, preserve
        # legacy 'extracted' idempotency (NULL identity → still CLAIM_DONE), and let
        # fresh archives claim normally.
        db = Path(self._tmp.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(db)
        legacy.execute(
            """
            CREATE TABLE extractions (
                archive_path TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                extracted_at REAL
            )
            """
        )
        legacy.execute(
            "INSERT INTO extractions (archive_path, status, claimed_at, extracted_at) VALUES (?, ?, ?, ?)",
            ("/data/rel/legacy.rar", "extracted", 10.0, 11.0),
        )
        legacy.commit()
        legacy.close()

        store = StateStore(db)
        columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(extractions)")}
        self.assertTrue({"size", "mtime", "token"} <= columns)

        # Legacy extracted row (no recorded identity) stays done — no surprise re-extract.
        self.assertEqual(
            store.claim_extraction(Path("/data/rel/legacy.rar"), 100.0, size=5, mtime=5.0).decision,
            CLAIM_DONE,
        )
        # A never-seen archive still claims cleanly on the migrated DB.
        self.assertEqual(
            store.claim_extraction(Path("/data/rel/new.rar"), 100.0, size=5, mtime=5.0).decision,
            CLAIM_NEW,
        )

    def test_protected_paths_respect_the_window(self) -> None:
        archive = Path("/data/rel/movie.rar")
        outputs = [Path("/data/rel/movie.mkv"), Path("/data/rel/movie.nfo")]
        claim = self._claim(archive, 1000.0)
        self.store.complete_extraction(archive, outputs, 1000.0, token=claim.token)

        within = self.store.get_protected_extracted_paths(1000.0 + 500, protect_seconds=1000)
        self.assertEqual(within, set(outputs))

        # Past the window they are no longer force-protected.
        after = self.store.get_protected_extracted_paths(1000.0 + 2000, protect_seconds=1000)
        self.assertEqual(after, set())

    def test_prune_forgets_expired_outputs_only(self) -> None:
        old = Path("/data/old/a.mkv")
        fresh = Path("/data/new/b.mkv")
        old_claim = self._claim(Path("/data/old/a.rar"), 0.0)
        self.store.complete_extraction(Path("/data/old/a.rar"), [old], 0.0, token=old_claim.token)
        fresh_claim = self._claim(Path("/data/new/b.rar"), 5000.0)
        self.store.complete_extraction(Path("/data/new/b.rar"), [fresh], 5000.0, token=fresh_claim.token)

        self.store.prune_extraction_outputs(6000.0, protect_seconds=2000)

        remaining = self.store.get_protected_extracted_paths(6000.0, protect_seconds=10_000)
        self.assertEqual(remaining, {fresh})


class ConcurrencyTests(unittest.TestCase):
    """WAL journaling + busy-timeout + lock-retry hardening (#45)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _journal_mode(self, store: StateStore) -> str:
        return store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower()

    def test_wal_enabled_on_a_fresh_db(self) -> None:
        store = StateStore(Path(self._tmp.name) / "fresh.sqlite3")
        self.assertEqual(self._journal_mode(store), "wal")

    def test_wal_enabled_on_an_existing_legacy_db(self) -> None:
        db = Path(self._tmp.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(db)
        legacy.execute(
            """
            CREATE TABLE extractions (
                archive_path TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                extracted_at REAL
            )
            """
        )
        legacy.commit()
        legacy.close()
        store = StateStore(db)
        self.assertEqual(self._journal_mode(store), "wal")

    def test_busy_timeout_is_raised_on_the_connection(self) -> None:
        # The raised busy timeout is what makes a claim wait for a slow complete
        # instead of failing. SQLite reports it in milliseconds.
        store = StateStore(Path(self._tmp.name) / "bt.sqlite3")
        busy_ms = store._connection.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(busy_ms, 30000)

    def test_claim_waits_for_a_briefly_held_write_lock(self) -> None:
        # #45: a claim racing a slow complete (which holds the write lock) must
        # wait for it to finish and then win, not surface a spurious 'failed'.
        db = Path(self._tmp.name) / "contended.sqlite3"
        StateStore(db)._connection.close()  # bootstrap schema + WAL
        store = StateStore(db)  # generous default busy_timeout

        lock_held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            conn = sqlite3.connect(db, timeout=30)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO extractions (archive_path, status, claimed_at) VALUES (?, 'claimed', ?)",
                ("/data/other.rar", 1.0),
            )
            lock_held.set()
            release.wait(5)
            conn.commit()
            conn.close()

        worker = threading.Thread(target=holder)
        worker.start()
        try:
            self.assertTrue(lock_held.wait(5))  # writer now holds the lock
            # Free the lock shortly after; the claim below must block until then.
            releaser = threading.Timer(0.3, release.set)
            releaser.start()
            result = store.claim_extraction(Path("/data/movie.rar"), 1.0, size=1, mtime=1.0)
            self.assertEqual(result.decision, CLAIM_NEW)  # waited, then won
        finally:
            release.set()
            worker.join(5)

    def test_claim_surfaces_when_a_lock_outlasts_the_busy_timeout(self) -> None:
        # busy_timeout=0 → no wait: a held write lock raises immediately. This is
        # the documented tail (past the timeout the OperationalError propagates and
        # Extractor.extract_all retries the archive next cycle).
        db = Path(self._tmp.name) / "starved.sqlite3"
        StateStore(db)._connection.close()  # bootstrap schema + WAL
        store = StateStore(db, busy_timeout_seconds=0)

        blocker = sqlite3.connect(db, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO extractions (archive_path, status, claimed_at) VALUES (?, 'claimed', ?)",
            ("/data/other.rar", 1.0),
        )
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.claim_extraction(Path("/data/movie.rar"), 1.0, size=1, mtime=1.0)
        finally:
            blocker.rollback()
            blocker.close()


class RecentWebReclaimActionsTests(unittest.TestCase):
    """``StateStore.recent_web_reclaim_actions`` — the per-path audit lookup backing the
    #74 staging-reconciliation disambiguation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store._connection.close()
        self._tmp.cleanup()

    def test_filters_by_path_and_web_reclaim_action_newest_first(self) -> None:
        self.store.record_actions(
            [
                ActionRecord(path=Path("/lib/a.mkv"), action="web-reclaim:filesystem", status="error", size=5, message="older"),
            ],
            100.0,
        )
        self.store.record_actions(
            [
                ActionRecord(path=Path("/lib/a.mkv"), action="web-reclaim:reconcile", status="removed", size=5, message="newer"),
                ActionRecord(path=Path("/lib/other.mkv"), action="web-reclaim:filesystem", status="deleted", size=3, message="other-path"),
                ActionRecord(path=Path("/lib/a.mkv"), action="delete", status="deleted", size=1, message="cleaner-not-web"),
            ],
            200.0,
        )
        rows = self.store.recent_web_reclaim_actions(Path("/lib/a.mkv"))
        # Only web-reclaim:* rows for THIS path, newest first; the cleaner "delete" row
        # and the other path are excluded.
        self.assertEqual([r["message"] for r in rows], ["newer", "older"])
        self.assertTrue(all(r["path"] == "/lib/a.mkv" for r in rows))
        self.assertTrue(all(r["action"].startswith("web-reclaim:") for r in rows))

    def test_missing_path_returns_empty(self) -> None:
        self.assertEqual(self.store.recent_web_reclaim_actions(Path("/nope.mkv")), [])

    def test_limit_is_bounded(self) -> None:
        self.store.record_actions(
            [
                ActionRecord(path=Path("/lib/a.mkv"), action="web-reclaim:filesystem", status="deleted", size=i, message=str(i))
                for i in range(5)
            ],
            100.0,
        )
        self.assertEqual(len(self.store.recent_web_reclaim_actions(Path("/lib/a.mkv"), limit=2)), 2)
        self.assertEqual(len(self.store.recent_web_reclaim_actions(Path("/lib/a.mkv"), limit=0)), 0)


class WebActionHistoryReaderTests(unittest.TestCase):
    """The read-only history reader backing ``/api/actions`` (#62)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "state.sqlite3"
        self._readers: list[WebActionHistoryReader] = []

    def tearDown(self) -> None:
        for reader in self._readers:
            if reader._conn is not None:
                reader._conn.close()
        self._tmp.cleanup()

    def _reader(self, **kw) -> WebActionHistoryReader:
        reader = WebActionHistoryReader(self.db, **kw)
        self._readers.append(reader)
        return reader

    def _write(self, rows, now):
        store = StateStore(self.db)
        store.record_actions(rows, now)
        store._connection.close()

    def test_missing_db_is_unavailable_and_creates_nothing(self) -> None:
        self.assertIsNone(self._reader()())
        # A read of a missing DB must not create the file (read-only viewer).
        self.assertFalse(self.db.exists())

    def test_legacy_db_without_actions_table_is_unavailable(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        self.assertIsNone(self._reader()())

    def test_empty_but_present_table_is_available_and_empty(self) -> None:
        StateStore(self.db)._connection.close()  # creates the actions table, no rows
        self.assertEqual(self._reader()(), [])

    def test_corrupt_non_sqlite_file_is_unavailable_not_raise(self) -> None:
        # A non-SQLite file at the path raises sqlite3.DatabaseError (parent of
        # OperationalError); the reader degrades to None, not a crash.
        self.db.write_bytes(b"this is not a database")
        self.assertIsNone(self._reader()())

    def test_read_does_not_write_the_main_db(self) -> None:
        # A reused query-only connection means a page load is a pure SELECT — no
        # checkpoint or migration touches the main DB file (no-write-on-GET).
        self._write(
            [ActionRecord(path=Path("/lib/a.mkv"), action="web-reclaim:filesystem", status="deleted", size=1)],
            now=100.0,
        )
        reader = self._reader()
        before = self.db.stat().st_mtime_ns
        self.assertEqual(len(reader()), 1)
        self.assertEqual(len(reader()), 1)  # a second GET reuses the same connection
        self.assertEqual(self.db.stat().st_mtime_ns, before)

    def test_query_only_connection_refuses_writes(self) -> None:
        self._write(
            [ActionRecord(path=Path("/lib/a.mkv"), action="web-reclaim:filesystem", status="deleted", size=1)],
            now=100.0,
        )
        reader = self._reader()
        reader()  # opens the connection
        with self.assertRaises(sqlite3.OperationalError):
            reader._conn.execute("DELETE FROM actions")

    def test_returns_only_web_reclaim_rows_newest_first(self) -> None:
        self._write(
            [
                ActionRecord(path=Path("/lib/a.mkv"), action="web-reclaim:filesystem", status="deleted", size=5),
            ],
            now=100.0,
        )
        self._write(
            [
                ActionRecord(path=Path("/data/orphan.mkv"), action="delete", status="deleted", size=9),
                ActionRecord(path=Path("/lib/b.mkv"), action="web-reclaim:radarr", status="deleted", size=7),
            ],
            now=200.0,
        )
        rows = self._reader()()
        self.assertIsNotNone(rows)
        # The cleaner's own "delete" row is filtered out; only web-reclaim survives.
        actions = [r["action"] for r in rows]
        self.assertEqual(actions, ["web-reclaim:radarr", "web-reclaim:filesystem"])  # newest first
        self.assertEqual(rows[0]["path"], "/lib/b.mkv")
        self.assertEqual(rows[0]["size"], 7)
        self.assertEqual(rows[0]["occurred_at"], 200.0)

    def test_limit_bounds_and_zero_is_unavailable(self) -> None:
        self._write(
            [
                ActionRecord(path=Path(f"/lib/{i}.mkv"), action="web-reclaim:filesystem", status="deleted", size=i)
                for i in range(5)
            ],
            now=100.0,
        )
        self.assertEqual(len(self._reader(limit=2)()), 2)
        self.assertIsNone(self._reader(limit=0)())

    def test_read_is_thread_safe_and_sees_a_concurrent_write(self) -> None:
        # One reader (a single lock-guarded connection) called from a worker thread
        # runs concurrently with an audit write on another connection (WAL) without
        # racing the shared connection or raising.
        self._write(
            [ActionRecord(path=Path("/lib/seed.mkv"), action="web-reclaim:filesystem", status="deleted", size=1)],
            now=50.0,
        )
        reader = self._reader()
        writer = StateStore(self.db, check_same_thread=False)
        results = []
        errors = []

        def read_loop():
            try:
                for _ in range(20):
                    results.append(reader())
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        thread = threading.Thread(target=read_loop)
        thread.start()
        for i in range(20):
            writer.record_actions(
                [ActionRecord(path=Path(f"/lib/w{i}.mkv"), action="web-reclaim:radarr", status="deleted", size=i)],
                now=100.0 + i,
            )
        thread.join(5)
        writer._connection.close()
        self.assertEqual(errors, [])
        self.assertTrue(all(r is not None for r in results))

    def test_index_created_for_history_query(self) -> None:
        StateStore(self.db)._connection.close()
        conn = sqlite3.connect(self.db)
        # PRAGMA index_list columns: (seq, name, unique, origin, partial).
        names = {row[1] for row in conn.execute("PRAGMA index_list('actions')")}
        conn.close()
        self.assertIn("ix_actions_occurred_at", names)


if __name__ == "__main__":
    unittest.main()
