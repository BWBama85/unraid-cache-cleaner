"""Extraction ledger (claim / idempotency / output protection) tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.extractor import CLAIM_BUSY, CLAIM_DONE, CLAIM_NEW
from unraid_cache_cleaner.state import StateStore


class ExtractionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_claim_is_new_then_done_after_complete(self) -> None:
        archive = Path("/data/rel/movie.rar")

        self.assertEqual(self.store.claim_extraction(archive, 1000.0), CLAIM_NEW)
        # A second claim while still 'claimed' is busy (owned by the first caller).
        self.assertEqual(self.store.claim_extraction(archive, 1000.0), CLAIM_BUSY)

        self.store.complete_extraction(archive, [Path("/data/rel/movie.mkv")], 1001.0)
        self.assertEqual(self.store.claim_extraction(archive, 1002.0), CLAIM_DONE)

    def test_release_lets_a_deferred_archive_retry(self) -> None:
        archive = Path("/data/rel/movie.rar")

        self.assertEqual(self.store.claim_extraction(archive, 1000.0), CLAIM_NEW)
        self.store.release_extraction(archive)
        # After release the claim is gone, so a later cycle wins a fresh claim.
        self.assertEqual(self.store.claim_extraction(archive, 1001.0), CLAIM_NEW)

    def test_release_never_drops_a_completed_record(self) -> None:
        archive = Path("/data/rel/movie.rar")
        self.store.claim_extraction(archive, 1000.0)
        self.store.complete_extraction(archive, [], 1001.0)

        self.store.release_extraction(archive)  # must be a no-op on 'extracted'

        self.assertEqual(self.store.claim_extraction(archive, 1002.0), CLAIM_DONE)

    def test_stale_claim_is_reclaimable(self) -> None:
        archive = Path("/data/rel/movie.rar")
        self.assertEqual(self.store.claim_extraction(archive, 1000.0, ttl_seconds=100), CLAIM_NEW)

        # Within the TTL: still busy. Past the TTL: reclaimable (crash recovery).
        self.assertEqual(self.store.claim_extraction(archive, 1050.0, ttl_seconds=100), CLAIM_BUSY)
        self.assertEqual(self.store.claim_extraction(archive, 1200.0, ttl_seconds=100), CLAIM_NEW)

    def test_protected_paths_respect_the_window(self) -> None:
        archive = Path("/data/rel/movie.rar")
        outputs = [Path("/data/rel/movie.mkv"), Path("/data/rel/movie.nfo")]
        self.store.claim_extraction(archive, 1000.0)
        self.store.complete_extraction(archive, outputs, 1000.0)

        within = self.store.get_protected_extracted_paths(1000.0 + 500, protect_seconds=1000)
        self.assertEqual(within, set(outputs))

        # Past the window they are no longer force-protected.
        after = self.store.get_protected_extracted_paths(1000.0 + 2000, protect_seconds=1000)
        self.assertEqual(after, set())

    def test_prune_forgets_expired_outputs_only(self) -> None:
        old = Path("/data/old/a.mkv")
        fresh = Path("/data/new/b.mkv")
        self.store.claim_extraction(Path("/data/old/a.rar"), 0.0)
        self.store.complete_extraction(Path("/data/old/a.rar"), [old], 0.0)
        self.store.claim_extraction(Path("/data/new/b.rar"), 5000.0)
        self.store.complete_extraction(Path("/data/new/b.rar"), [fresh], 5000.0)

        self.store.prune_extraction_outputs(6000.0, protect_seconds=2000)

        remaining = self.store.get_protected_extracted_paths(6000.0, protect_seconds=10_000)
        self.assertEqual(remaining, {fresh})


if __name__ == "__main__":
    unittest.main()
