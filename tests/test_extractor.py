"""Extractor tests (fake tool + one gated real-binary roundtrip)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.config import Config
from unraid_cache_cleaner.extractor import (
    Extractor,
    ExtractorError,
    UnarArchiveTool,
    _derive_list_tool,
    _parse_owner,
    _safe_member_path,
    summarize,
)
from unraid_cache_cleaner.models import CLAIM_NEW, ClaimResult
from unraid_cache_cleaner.planner import is_within, normalize_path
from unraid_cache_cleaner.state import StateExtractionLedger, StateStore


class _BoomLedger:
    """Ledger whose complete() raises, to test claim release on bookkeeping failure."""

    TOKEN = "boom-token"

    def __init__(self) -> None:
        self.released: list = []

    def claim(self, archive: Path, now: float, *, size: int, mtime: float) -> ClaimResult:
        return ClaimResult(CLAIM_NEW, self.TOKEN)

    def complete(self, archive: Path, outputs, now: float, *, token) -> None:
        raise sqlite3.OperationalError("database is locked")

    def release(self, archive: Path, *, token) -> None:
        self.released.append((archive, token))


class _RecordingLedger:
    """Grants every claim and records the token threaded into complete/release."""

    TOKEN = "tok-abc123"

    def __init__(self) -> None:
        self.completed: list = []
        self.released: list = []

    def claim(self, archive: Path, now: float, *, size: int, mtime: float) -> ClaimResult:
        return ClaimResult(CLAIM_NEW, self.TOKEN)

    def complete(self, archive: Path, outputs, now: float, *, token) -> None:
        self.completed.append((archive, token))

    def release(self, archive: Path, *, token) -> None:
        self.released.append((archive, token))


# Sentinel: FakeTool.list_members derives the member list from the archive name
# (mirroring what extract() writes) unless a test pins an explicit result.
_DERIVE_MEMBERS = object()


class FakeTool:
    """Stand-in for the injected archive tool; records calls, no subprocess."""

    def __init__(
        self,
        *,
        available: bool = True,
        test_result: bool = True,
        test_raises: Exception | None = None,
        extract_raises: Exception | None = None,
        fail_names: set[str] | None = None,
        list_members_result: object = _DERIVE_MEMBERS,
    ) -> None:
        self.available = available
        self.test_result = test_result
        self.test_raises = test_raises
        self.extract_raises = extract_raises
        self.fail_names = fail_names or set()
        self.list_members_result = list_members_result
        self.test_calls: list[Path] = []
        self.extract_calls: list[tuple[Path, Path]] = []
        self.list_members_calls: list[Path] = []

    def is_available(self) -> bool:
        return self.available

    def test(self, archive: Path) -> bool:
        self.test_calls.append(archive)
        if self.test_raises is not None:
            raise self.test_raises
        return self.test_result

    def _member(self, archive: Path) -> Path:
        """The archive-relative member ``extract`` writes and ``list_members`` reports.

        Subclasses override this alone to move the output (e.g. into a subdirectory);
        both paths stay in lockstep, as they are for a real tool.
        """

        return Path(Path(archive.name).stem + ".mkv")

    def extract(self, archive: Path, dest_dir: Path) -> None:
        self.extract_calls.append((archive, dest_dir))
        if self.extract_raises is not None:
            raise self.extract_raises
        if archive.name in self.fail_names:
            raise ExtractorError(f"boom: {archive.name}")
        # Simulate a real extraction so ownership/os.walk paths have something.
        target = dest_dir / self._member(archive)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("extracted")

    def list_members(self, archive: Path) -> list[Path] | None:
        self.list_members_calls.append(archive)
        if self.list_members_result is not _DERIVE_MEMBERS:
            return self.list_members_result  # type: ignore[return-value]
        # Mirror what extract() writes.
        return [self._member(archive)]


def _make_config(
    *,
    watch_root: Path,
    config_root: Path,
    dry_run: bool = False,
    extract_enabled: bool = True,
    extract_owner: str = "",
    extract_min_age_seconds: int = 0,
    excluded_globs: tuple[str, ...] = (),
) -> Config:
    return Config(
        qbittorrent_url="http://qbt:8080",
        qbittorrent_username="admin",
        qbittorrent_password="secret",
        qbittorrent_timeout_seconds=15,
        qbittorrent_verify_tls=True,
        watch_paths=(watch_root,),
        poll_interval_seconds=300,
        orphan_grace_seconds=0,
        min_file_age_seconds=0,
        dry_run=dry_run,
        delete_empty_dirs=True,
        protect_single_file_parent_dirs=True,
        excluded_globs=excluded_globs,
        state_db_path=config_root / "state.sqlite3",
        report_path=config_root / "last-run.json",
        log_level="INFO",
        plex_duplicate_report_path=config_root / "plex-duplicates.json",
        extract_enabled=extract_enabled,
        extract_owner=extract_owner,
        extract_min_age_seconds=extract_min_age_seconds,
    )


class _Fixture:
    """Temp dir with data/config roots; use as a context manager."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.watch_root = root / "data"
        self.config_root = root / "config"
        self.watch_root.mkdir()
        self.config_root.mkdir()

    def __enter__(self) -> "_Fixture":
        return self

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()

    def write_rar(self, relative: str, content: str = "rar") -> Path:
        path = self.watch_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def config(self, **overrides: object) -> Config:
        return _make_config(
            watch_root=self.watch_root, config_root=self.config_root, **overrides
        )


class ExtractorTests(unittest.TestCase):
    def test_extracts_single_archive(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("release/movie.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertEqual(len(tool.extract_calls), 1)
            self.assertTrue((fx.watch_root / "release" / "movie.mkv").exists())

    def test_multivolume_extracts_first_volume_only(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/show.part01.rar")
            fx.write_rar("rel/show.part02.rar")
            fx.write_rar("rel/show.part03.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual(len(results), 1)
            self.assertEqual(len(tool.extract_calls), 1)
            self.assertEqual(tool.extract_calls[0][0].name, "show.part01.rar")

    def test_legacy_rNN_volumes_are_not_scanned(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            fx.write_rar("rel/movie.r00")
            fx.write_rar("rel/movie.r01")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([c[0].name for c in tool.extract_calls], ["movie.rar"])

    def test_dry_run_reports_would_extract_without_writing(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(dry_run=True), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=True)

            self.assertEqual([r.status for r in results], ["would_extract"])
            self.assertEqual(tool.extract_calls, [])
            self.assertEqual(len(tool.test_calls), 1)
            self.assertFalse((fx.watch_root / "rel" / "movie.mkv").exists())

    def test_integrity_failure_defers(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            tool = FakeTool(test_result=False)
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["deferred_incomplete"])
            self.assertEqual(tool.extract_calls, [])

    def test_extraction_failure_marks_failed(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            tool = FakeTool(extract_raises=ExtractorError("unar exited 1"))
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["failed"])
            self.assertEqual(len(tool.extract_calls), 1)

    def test_settle_guard_defers_recent_archive(self) -> None:
        with _Fixture() as fx:
            archive = fx.write_rar("rel/movie.rar")
            now = 1_000_000.0
            os.utime(archive, (now - 10, now - 10))  # 10s old, younger than min age
            tool = FakeTool()
            extractor = Extractor(
                fx.config(extract_min_age_seconds=3600),
                tool=tool,
                clock=lambda: now,
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["deferred_incomplete"])
            self.assertEqual(tool.test_calls, [])  # deferred before the integrity test
            self.assertEqual(tool.extract_calls, [])

    def test_settle_guard_considers_legacy_rNN_volumes(self) -> None:
        # movie.rar is old, but a legacy continuation volume movie.r01 is still
        # fresh — the set is not settled, so extraction must defer.
        with _Fixture() as fx:
            rar = fx.write_rar("rel/movie.rar")
            r01 = fx.write_rar("rel/movie.r01")
            now = 1_000_000.0
            os.utime(rar, (now - 10_000, now - 10_000))
            os.utime(r01, (now - 10, now - 10))
            tool = FakeTool()
            extractor = Extractor(
                fx.config(extract_min_age_seconds=3600), tool=tool, clock=lambda: now
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["deferred_incomplete"])
            self.assertEqual(tool.extract_calls, [])

    def test_missing_binary_raises(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            extractor = Extractor(fx.config(), tool=FakeTool(available=False))

            with self.assertRaises(ExtractorError):
                extractor.extract_all((fx.watch_root,), dry_run=False)

    def test_symlinked_archive_is_skipped(self) -> None:
        with _Fixture() as fx:
            real = fx.write_rar("real/movie.rar")
            os.symlink(real, fx.watch_root / "alias.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual(
                [c[0] for c in tool.extract_calls], [normalize_path(real)]
            )

    def test_per_archive_error_isolation(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("a/good.rar")
            fx.write_rar("b/bad.rar")
            tool = FakeTool(fail_names={"bad.rar"})
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            by_name = {r.archive.name: r.status for r in results}
            self.assertEqual(by_name, {"good.rar": "extracted", "bad.rar": "failed"})

    def test_ownership_applied_only_to_new_files(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")  # pre-existing; must NOT be chowned
            chown_calls: list[tuple[str, int, int]] = []
            extractor = Extractor(
                fx.config(extract_owner="99:100"),
                tool=FakeTool(),
                chown=lambda path, uid, gid: chown_calls.append((path, uid, gid)),
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertTrue(all((uid, gid) == (99, 100) for _, uid, gid in chown_calls))
            chowned = {path for path, _, _ in chown_calls}
            self.assertIn(str(fx.watch_root / "rel" / "movie.mkv"), chowned)  # extracted
            self.assertNotIn(str(fx.watch_root / "rel" / "movie.rar"), chowned)  # source

    def test_ownership_scoped_to_extracted_output_at_watch_root(self) -> None:
        # A loose .rar at the watch root must not turn chown into an
        # entire-mount ownership rewrite of unrelated siblings.
        with _Fixture() as fx:
            fx.write_rar("loose.rar")
            (fx.watch_root / "unrelated.mkv").write_text("keep")
            chown_calls: list[str] = []
            extractor = Extractor(
                fx.config(extract_owner="99:100"),
                tool=FakeTool(),
                chown=lambda path, uid, gid: chown_calls.append(path),
            )

            extractor.extract_all((fx.watch_root,), dry_run=False)

            chowned = set(chown_calls)
            self.assertIn(str(fx.watch_root / "loose.mkv"), chowned)  # extracted output
            self.assertNotIn(str(fx.watch_root / "unrelated.mkv"), chowned)
            self.assertNotIn(str(fx.watch_root / "loose.rar"), chowned)

    def test_ownership_never_follows_symlinks(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            external = Path(fx._tmp.name) / "external.txt"  # out of the watch tree
            external.write_text("external")

            class LinkTool(FakeTool):
                def extract(self, archive: Path, dest_dir: Path) -> None:
                    super().extract(archive, dest_dir)
                    os.symlink(external, dest_dir / "link.txt")

            chown_calls: list[str] = []
            extractor = Extractor(
                fx.config(extract_owner="99:100"),
                tool=LinkTool(),
                chown=lambda path, uid, gid: chown_calls.append(path),
            )

            extractor.extract_all((fx.watch_root,), dry_run=False)

            chowned = set(chown_calls)
            self.assertIn(str(fx.watch_root / "rel" / "movie.mkv"), chowned)
            self.assertNotIn(str(fx.watch_root / "rel" / "link.txt"), chowned)

    def test_chown_failure_is_non_fatal(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")

            def _boom(path: str, uid: int, gid: int) -> None:
                raise PermissionError("operation not permitted")

            extractor = Extractor(
                fx.config(extract_owner="99:100"), tool=FakeTool(), chown=_boom
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])

    def test_invalid_owner_skips_chown(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            chown_calls: list[object] = []
            extractor = Extractor(
                fx.config(extract_owner="nobody:users"),
                tool=FakeTool(),
                chown=lambda *a: chown_calls.append(a),
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertEqual(chown_calls, [])

    def test_summarize_counts_statuses(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("a/good.rar")
            fx.write_rar("b/bad.rar")
            tool = FakeTool(fail_names={"bad.rar"})
            extractor = Extractor(fx.config(), tool=tool)

            counts = summarize(extractor.extract_all((fx.watch_root,), dry_run=False))

            self.assertEqual(counts["extracted"], 1)
            self.assertEqual(counts["failed"], 1)


class MultiVolumeTests(unittest.TestCase):
    """First-volume-only selection across the layouts scene releases use (#37)."""

    def _extracted_names(self, layout: list[str]) -> list[str]:
        with _Fixture() as fx:
            for name in layout:
                fx.write_rar(name)
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)
            extractor.extract_all((fx.watch_root,), dry_run=False)
            return sorted(c[0].name for c in tool.extract_calls)

    def test_modern_partNN_first_volume_only(self) -> None:
        self.assertEqual(
            self._extracted_names(["rel/show.part01.rar", "rel/show.part02.rar", "rel/show.part03.rar"]),
            ["show.part01.rar"],
        )

    def test_mixed_case_part_suffix(self) -> None:
        self.assertEqual(
            self._extracted_names(["rel/Show.PART01.RAR", "rel/Show.Part02.Rar"]),
            ["Show.PART01.RAR"],
        )

    def test_single_digit_part(self) -> None:
        self.assertEqual(
            self._extracted_names(["rel/show.part1.rar", "rel/show.part2.rar"]),
            ["show.part1.rar"],
        )

    def test_three_digit_part(self) -> None:
        self.assertEqual(
            self._extracted_names(["rel/show.part001.rar", "rel/show.part002.rar"]),
            ["show.part001.rar"],
        )

    def test_double_digit_parts_sort_numerically_not_lexically(self) -> None:
        layout = [f"rel/show.part{n:02d}.rar" for n in range(1, 12)]  # part01..part11
        self.assertEqual(self._extracted_names(layout), ["show.part01.rar"])

    def test_first_volume_missing_is_not_extracted(self) -> None:
        # Only part02/part03 present (part01 still downloading): extract nothing.
        self.assertEqual(
            self._extracted_names(["rel/show.part02.rar", "rel/show.part03.rar"]),
            [],
        )

    def test_legacy_rNN_extracts_the_rar(self) -> None:
        self.assertEqual(
            self._extracted_names(["rel/movie.rar", "rel/movie.r00", "rel/movie.r01"]),
            ["movie.rar"],
        )

    def test_split_across_directories_only_extracts_the_first_dirs_volume(self) -> None:
        # A set split across dirs: the dir holding part01 extracts; the dir whose
        # lowest volume is part02 is treated as first-volume-missing and skipped.
        self.assertEqual(
            self._extracted_names(["a/show.part01.rar", "b/show.part02.rar"]),
            ["show.part01.rar"],
        )


class LedgerIdempotencyTests(unittest.TestCase):
    """Cross-run idempotency and output tracking via the SQLite ledger (#35)."""

    def _extractor(self, fx: "_Fixture", store: StateStore, **overrides: object) -> Extractor:
        return Extractor(fx.config(**overrides), tool=FakeTool(), ledger=StateExtractionLedger(store))

    def test_second_cycle_skips_without_reinvoking(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            store = StateStore(fx.config().state_db_path)

            first = Extractor(fx.config(), tool=(tool1 := FakeTool()), ledger=StateExtractionLedger(store))
            self.assertEqual([r.status for r in first.extract_all((fx.watch_root,), dry_run=False)], ["extracted"])
            self.assertEqual(len(tool1.extract_calls), 1)

            second = Extractor(fx.config(), tool=(tool2 := FakeTool()), ledger=StateExtractionLedger(store))
            results = second.extract_all((fx.watch_root,), dry_run=False)
            self.assertEqual([r.status for r in results], ["skipped_present"])
            self.assertEqual(tool2.extract_calls, [])  # no re-invoke
            self.assertEqual(tool2.test_calls, [])  # not even the integrity test

    def test_failure_is_not_recorded_and_retries(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            store = StateStore(fx.config().state_db_path)

            first = Extractor(
                fx.config(), tool=FakeTool(fail_names={"movie.rar"}), ledger=StateExtractionLedger(store)
            )
            self.assertEqual([r.status for r in first.extract_all((fx.watch_root,), dry_run=False)], ["failed"])

            # The released claim lets the next cycle try again (and succeed).
            second = Extractor(fx.config(), tool=(tool2 := FakeTool()), ledger=StateExtractionLedger(store))
            self.assertEqual([r.status for r in second.extract_all((fx.watch_root,), dry_run=False)], ["extracted"])
            self.assertEqual(len(tool2.extract_calls), 1)

    def test_extraction_records_protected_output_files(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            store = StateStore(fx.config().state_db_path)
            extractor = Extractor(fx.config(), tool=FakeTool(), ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            mkv = normalize_path(fx.watch_root / "rel" / "movie.mkv")
            self.assertIn(mkv, results[0].outputs)
            protected = store.get_protected_extracted_paths(0.0, protect_seconds=10**9)
            self.assertIn(mkv, protected)

    def test_dry_run_writes_no_ledger_state(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            store = StateStore(fx.config().state_db_path)
            extractor = Extractor(fx.config(dry_run=True), tool=FakeTool(), ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=True)

            self.assertEqual([r.status for r in results], ["would_extract"])
            # No claim and no outputs were persisted by the preview run.
            self.assertEqual(store.get_protected_extracted_paths(0.0, protect_seconds=10**9), set())

    def test_overwritten_output_is_still_protected(self) -> None:
        # A partial `movie.mkv` from a failed prior attempt (or an already-unpacked
        # file) exists before extraction; extraction overwrites it. Path-diff alone
        # would miss it — the mtime change must still record it as protected output.
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            stale = fx.write_rar("rel/movie.mkv", content="partial")
            os.utime(stale, (1000.0, 1000.0))  # old mtime; extraction rewrites it
            store = StateStore(fx.config().state_db_path)
            extractor = Extractor(fx.config(), tool=FakeTool(), ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            mkv = normalize_path(fx.watch_root / "rel" / "movie.mkv")
            self.assertIn(mkv, results[0].outputs)
            self.assertIn(mkv, store.get_protected_extracted_paths(0.0, protect_seconds=10**9))

    def test_overwrite_with_preserved_mtime_detected_by_size(self) -> None:
        # Real archive tools restore member timestamps, so an overwrite can keep the
        # same mtime. The (mtime, size) fingerprint must still catch a content change.
        class _MtimePreservingTool(FakeTool):
            def extract(self, archive: Path, dest_dir: Path) -> None:
                target = dest_dir / (Path(archive.name).stem + ".mkv")
                old = target.stat().st_mtime if target.exists() else None
                target.write_text("fully-extracted-content-much-larger-than-before")
                if old is not None:
                    os.utime(target, (old, old))  # restore mtime (member-timestamp preservation)

        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            fx.write_rar("rel/movie.mkv", content="x")  # 1 byte; same name as output
            store = StateStore(fx.config().state_db_path)
            extractor = Extractor(
                fx.config(), tool=_MtimePreservingTool(), ledger=StateExtractionLedger(store)
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            mkv = normalize_path(fx.watch_root / "rel" / "movie.mkv")
            self.assertIn(mkv, results[0].outputs)  # caught by the size delta

    def test_bookkeeping_failure_releases_claim_and_still_surfaces_outputs(self) -> None:
        # If recording the extraction raises (e.g. the ledger DB is locked by a
        # concurrent run), the media is on disk but unrecorded: the claim must be
        # released so the next cycle re-extracts and re-records (not wedged for the
        # TTL), AND the produced paths must still be surfaced so the caller can
        # protect them this cycle rather than deleting them as orphans.
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            ledger = _BoomLedger()
            extractor = Extractor(fx.config(), tool=FakeTool(), ledger=ledger)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["failed"])
            self.assertEqual(len(ledger.released), 1)  # claim released → retryable
            # The release is issued under the token the claim handed back (#41).
            self.assertEqual(ledger.released[0][1], _BoomLedger.TOKEN)
            mkv = normalize_path(fx.watch_root / "rel" / "movie.mkv")
            self.assertIn(mkv, results[0].outputs)  # protected this cycle despite failure

    def test_claim_token_is_threaded_into_complete(self) -> None:
        # The token a live claim returns must reach complete() on the success path so
        # the ledger can prove ownership before promoting/recording (#41).
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            ledger = _RecordingLedger()
            extractor = Extractor(fx.config(), tool=FakeTool(), ledger=ledger)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertEqual(len(ledger.completed), 1)
            self.assertEqual(ledger.completed[0][1], _RecordingLedger.TOKEN)
            self.assertEqual(ledger.released, [])  # nothing to release on success

    def test_reused_path_with_different_archive_reextracts(self) -> None:
        # #41 fix 1, end to end: a different archive later written to a
        # previously-extracted path re-invokes the tool instead of skipped_present.
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar", content="first-archive")
            store = StateStore(fx.config().state_db_path)

            first = Extractor(fx.config(), tool=(tool1 := FakeTool()), ledger=StateExtractionLedger(store))
            self.assertEqual([r.status for r in first.extract_all((fx.watch_root,), dry_run=False)], ["extracted"])
            self.assertEqual(len(tool1.extract_calls), 1)

            # A genuinely different archive lands at the same path (different size).
            fx.write_rar("rel/movie.rar", content="a-different-and-larger-second-archive")
            second = Extractor(fx.config(), tool=(tool2 := FakeTool()), ledger=StateExtractionLedger(store))
            results = second.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])  # not skipped_present
            self.assertEqual(len(tool2.extract_calls), 1)  # re-invoked

    def test_changed_continuation_volume_reextracts_the_set(self) -> None:
        # A multi-volume set is keyed on part01, but identity uses the set's newest
        # mtime: if a continuation volume is replaced (part01 unchanged), the set is
        # re-extracted rather than wrongly skipped as CLAIM_DONE.
        clock = lambda: 2_000_000.0  # noqa: E731 - far ahead of the fixture mtimes
        with _Fixture() as fx:
            part01 = fx.write_rar("rel/show.part01.rar")
            part02 = fx.write_rar("rel/show.part02.rar")
            os.utime(part01, (1_000_000, 1_000_000))
            os.utime(part02, (1_000_000, 1_000_000))
            store = StateStore(fx.config().state_db_path)

            first = Extractor(
                fx.config(), tool=(tool1 := FakeTool()), ledger=StateExtractionLedger(store), clock=clock
            )
            self.assertEqual([r.status for r in first.extract_all((fx.watch_root,), dry_run=False)], ["extracted"])
            self.assertEqual(len(tool1.extract_calls), 1)

            # Only the continuation volume changes (newer mtime); part01 is untouched.
            os.utime(part02, (1_500_000, 1_500_000))
            second = Extractor(
                fx.config(), tool=(tool2 := FakeTool()), ledger=StateExtractionLedger(store), clock=clock
            )
            results = second.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])  # not skipped_present
            self.assertEqual(len(tool2.extract_calls), 1)  # re-invoked


class _IdenticalOverwriteTool(FakeTool):
    """Overwrites the output with byte-identical content *and* a restored mtime.

    This is the one overwrite the ``(mtime, size)`` diff cannot see (#43): a real
    archive tool preserves member timestamps, so re-extracting a file that already
    holds the archive's exact bytes leaves the fingerprint untouched. Only the
    archive's member list reveals that the file is a produced output.
    """

    CONTENT = "byte-identical-extracted-content"

    def extract(self, archive: Path, dest_dir: Path) -> None:
        self.extract_calls.append((archive, dest_dir))
        target = dest_dir / self._member(archive)
        target.parent.mkdir(parents=True, exist_ok=True)
        old_mtime = target.stat().st_mtime if target.exists() else None
        target.write_text(self.CONTENT)
        if old_mtime is not None:
            os.utime(target, (old_mtime, old_mtime))  # restore member timestamp


class _NestedIdenticalOverwriteTool(_IdenticalOverwriteTool):
    """The nested twin of :class:`_IdenticalOverwriteTool` (#54).

    Real archives spell a nested member with a backslash (``sub\\deep.mkv``) and
    ``unar`` splits on it to recreate the tree, so the member ``lsar`` reports —
    normalized to ``sub/deep.mkv`` — is exactly where the file lands. Nesting
    therefore costs the member-list mapping no precision.
    """

    MEMBER = Path("sub/deep.mkv")

    def _member(self, archive: Path) -> Path:
        return self.MEMBER


class MemberListOutputTests(unittest.TestCase):
    """Precise produced-output detection via the archive's member list (#43, #54)."""

    def _seed_identical_output(self, fx: "_Fixture") -> Path:
        """Pre-seed movie.mkv with the exact bytes+mtime a re-extract will restore."""

        fx.write_rar("rel/movie.rar")
        mkv = fx.write_rar("rel/movie.mkv", content=_IdenticalOverwriteTool.CONTENT)
        os.utime(mkv, (1000.0, 1000.0))
        return normalize_path(fx.watch_root / "rel" / "movie.mkv")

    def test_byte_identical_overwrite_recorded_via_member_list(self) -> None:
        with _Fixture() as fx:
            mkv = self._seed_identical_output(fx)
            before = os.stat(mkv)
            store = StateStore(fx.config().state_db_path)
            extractor = Extractor(
                fx.config(), tool=_IdenticalOverwriteTool(), ledger=StateExtractionLedger(store)
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            after = os.stat(mkv)
            # The fingerprint is genuinely unchanged: the diff alone is blind here.
            self.assertEqual((before.st_mtime, before.st_size), (after.st_mtime, after.st_size))
            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertIn(mkv, results[0].outputs)  # caught only by the member list
            self.assertIn(mkv, store.get_protected_extracted_paths(0.0, protect_seconds=10**9))

    def test_nested_byte_identical_overwrite_recorded_via_member_list(self) -> None:
        # #54: the same blind spot one directory down. The nested member maps to the
        # exact path `unar` writes, so it is recorded and protected like a root one.
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            deep = normalize_path(
                fx.write_rar("rel/sub/deep.mkv", content=_NestedIdenticalOverwriteTool.CONTENT)
            )
            os.utime(deep, (1000.0, 1000.0))
            before = os.stat(deep)
            store = StateStore(fx.config().state_db_path)
            extractor = Extractor(
                fx.config(),
                tool=_NestedIdenticalOverwriteTool(),
                ledger=StateExtractionLedger(store),
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            after = os.stat(deep)
            # The fingerprint is genuinely unchanged: the diff alone is blind here.
            self.assertEqual((before.st_mtime, before.st_size), (after.st_mtime, after.st_size))
            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertIn(deep, results[0].outputs)  # caught only by the member list
            self.assertIn(deep, store.get_protected_extracted_paths(0.0, protect_seconds=10**9))

    def test_member_naming_an_absent_path_is_never_invented(self) -> None:
        # A member that maps to no walked path is simply dropped. This is the shape
        # of the one case that stays uncovered (#54): a member stored with a literal
        # `/` is not a path to `unar`, which sanitizes it to a flat name
        # (`sub_deep.mkv` on Linux, `sub:deep.mkv` on macOS) that `lsar`'s normalized
        # `sub/deep.mkv` cannot address. The member list only ever confirms paths the
        # walk already found — it never conjures one, so the fallback stays safe.
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            store = StateStore(fx.config().state_db_path)
            tool = FakeTool(list_members_result=[Path("sub/deep.mkv")])
            extractor = Extractor(fx.config(), tool=tool, ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            outputs = {p for r in results for p in r.outputs}
            self.assertNotIn(normalize_path(fx.watch_root / "rel" / "sub" / "deep.mkv"), outputs)
            self.assertFalse((fx.watch_root / "rel" / "sub").exists())
            # ...while the genuinely produced file is still caught by the diff.
            self.assertIn(normalize_path(fx.watch_root / "rel" / "movie.mkv"), outputs)

    def test_nested_member_behind_symlinked_dir_is_never_recorded(self) -> None:
        # #54 must not let nesting open a symlink escape: `os.walk` does not follow
        # symlinked dirs, so a member named `sub/deep.mkv` whose `sub` is a symlink
        # out of the tree matches no walked path, and the file it points at is
        # neither recorded nor chowned.
        with _Fixture() as fx:
            outside = fx.watch_root.parent / "outside"
            outside.mkdir()
            (outside / "deep.mkv").write_text("a file outside the watch root")
            rel = fx.watch_root / "rel"
            rel.mkdir()
            (rel / "movie.rar").write_text("rar")
            os.symlink(outside, rel / "sub")  # pre-existing symlinked directory
            store = StateStore(fx.config().state_db_path)
            tool = FakeTool(list_members_result=[Path("sub/deep.mkv")])
            extractor = Extractor(fx.config(), tool=tool, ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            outputs = {p for r in results for p in r.outputs}
            self.assertNotIn(normalize_path(rel / "sub" / "deep.mkv"), outputs)
            for path in outputs:
                self.assertTrue(is_within(path, fx.watch_root), f"{path} escaped dest dir")
            self.assertEqual((outside / "deep.mkv").read_text(), "a file outside the watch root")

    def test_fallback_to_fingerprint_when_members_unavailable(self) -> None:
        # With member enumeration unavailable, the byte-identical overwrite reverts
        # to being invisible (documents the fallback), while a genuinely new file is
        # still detected — the fingerprint diff is intact.
        with _Fixture() as fx:
            mkv = self._seed_identical_output(fx)
            fx.write_rar("rel/extra.rar")  # produces extra.mkv (a brand-new file)
            store = StateStore(fx.config().state_db_path)
            tool = _IdenticalOverwriteTool(list_members_result=None)
            extractor = Extractor(fx.config(), tool=tool, ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertTrue(tool.list_members_calls)  # enumeration was attempted
            outputs = {p for r in results for p in r.outputs}
            self.assertNotIn(mkv, outputs)  # blind spot: unchanged fingerprint, no member list
            self.assertIn(normalize_path(fx.watch_root / "rel" / "extra.mkv"), outputs)  # new file still caught

    def test_member_outside_dest_dir_is_never_recorded(self) -> None:
        # A malicious/malformed archive naming a traversal member must not resolve a
        # produced-output path outside dest_dir.
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            store = StateStore(fx.config().state_db_path)
            tool = FakeTool(list_members_result=[Path("../escape.mkv"), Path("/abs/escape.mkv")])
            extractor = Extractor(fx.config(), tool=tool, ledger=StateExtractionLedger(store))

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            for path in {p for r in results for p in r.outputs}:
                self.assertTrue(is_within(path, fx.watch_root), f"{path} escaped dest dir")
            self.assertFalse((fx.watch_root.parent / "escape.mkv").exists())

    def test_safe_member_path_rejects_traversal_and_absolute(self) -> None:
        dest = Path("/data/rel")
        self.assertEqual(_safe_member_path(dest, Path("movie.mkv")), normalize_path(dest / "movie.mkv"))
        self.assertEqual(
            _safe_member_path(dest, Path("sub/movie.mkv")), normalize_path(dest / "sub" / "movie.mkv")
        )
        self.assertIsNone(_safe_member_path(dest, Path("../escape.mkv")))
        self.assertIsNone(_safe_member_path(dest, Path("sub/../../escape.mkv")))
        self.assertIsNone(_safe_member_path(dest, Path("/abs/escape.mkv")))


class IncompleteRootsTests(unittest.TestCase):
    def test_archive_under_incomplete_torrent_is_deferred(self) -> None:
        with _Fixture() as fx:
            archive = fx.write_rar("rel/movie.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all(
                (fx.watch_root,),
                dry_run=False,
                incomplete_roots=(archive.parent,),
            )

            self.assertEqual([r.status for r in results], ["deferred_incomplete"])
            self.assertEqual(tool.test_calls, [])  # deferred before the integrity test
            self.assertEqual(tool.extract_calls, [])


class HelperTests(unittest.TestCase):
    def test_derive_list_tool(self) -> None:
        self.assertEqual(_derive_list_tool("unar"), "lsar")
        self.assertEqual(_derive_list_tool("/usr/bin/unar"), "/usr/bin/lsar")
        self.assertIsNone(_derive_list_tool("p7zip"))

    def test_parse_owner(self) -> None:
        self.assertEqual(_parse_owner("99:100"), (99, 100))
        with self.assertRaises(ValueError):
            _parse_owner("99")
        with self.assertRaises(ValueError):
            _parse_owner("nobody:users")


class UnarArchiveToolTests(unittest.TestCase):
    """Exercise command construction and error mapping without the real binary."""

    def _runner(self, returncode: int, *, stderr: str = "", stdout: str = ""):
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        return runner, calls

    def test_extract_builds_command_and_succeeds(self) -> None:
        runner, calls = self._runner(0)
        tool = UnarArchiveTool("unar", runner=runner)

        tool.extract(Path("/data/rel/movie.rar"), Path("/data/rel"))

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[0], "unar")
        self.assertIn("-output-directory", cmd)
        self.assertEqual(cmd[cmd.index("-output-directory") + 1], "/data/rel")
        self.assertEqual(cmd[-1], "/data/rel/movie.rar")
        # nested archives must not be auto-extracted (mirror `unrar x`)
        self.assertIn("-no-recursion", cmd)
        # Suppresses only unar's *archive-named enclosing* dir, so contents land in
        # place; it does not flatten members, which keep their nesting (#54).
        self.assertIn("-no-directory", cmd)

    def test_test_uses_integrity_test_flag(self) -> None:
        runner, calls = self._runner(0)
        # list_tool must resolve on PATH for the pre-test to run; sys.executable does.
        tool = UnarArchiveTool("unar", list_tool=sys.executable, runner=runner)

        self.assertTrue(tool.test(Path("/data/rel/movie.rar")))
        self.assertEqual(len(calls), 1)
        self.assertIn("-test", calls[0])  # not a plain header listing
        self.assertEqual(calls[0][-1], "/data/rel/movie.rar")

    def test_test_returns_false_on_nonzero(self) -> None:
        runner, _ = self._runner(1)
        tool = UnarArchiveTool("unar", list_tool=sys.executable, runner=runner)

        self.assertFalse(tool.test(Path("/data/rel/movie.rar")))

    def test_extract_raises_on_nonzero_exit(self) -> None:
        runner, _ = self._runner(2, stderr="Couldn't open archive")
        tool = UnarArchiveTool("unar", runner=runner)

        with self.assertRaises(ExtractorError) as ctx:
            tool.extract(Path("/data/rel/movie.rar"), Path("/data/rel"))
        self.assertIn("Couldn't open archive", str(ctx.exception))

    def test_test_skipped_when_no_list_tool(self) -> None:
        runner, calls = self._runner(1)
        # A non-unar binary yields no derived lsar, so the pre-test is skipped.
        tool = UnarArchiveTool("p7zip", runner=runner)

        self.assertTrue(tool.test(Path("/data/rel/movie.rar")))
        self.assertEqual(calls, [])

    def test_list_members_parses_json_and_drops_directories(self) -> None:
        payload = json.dumps(
            {
                "lsarContents": [
                    {"XADFileName": "movie.mkv", "XADFileSize": 10},
                    {"XADFileName": "subs", "XADIsDirectory": 1},
                    {"XADFileName": "sub/movie.srt"},
                ]
            }
        )
        runner, calls = self._runner(0, stdout=payload)
        tool = UnarArchiveTool("unar", list_tool=sys.executable, runner=runner)

        members = tool.list_members(Path("/data/rel/movie.rar"))

        self.assertEqual(members, [Path("movie.mkv"), Path("sub/movie.srt")])
        self.assertIn("-json", calls[0])
        # Mirror extract()'s -no-recursion: don't list an inner archive's members.
        self.assertIn("-no-recursion", calls[0])
        self.assertEqual(calls[0][-1], "/data/rel/movie.rar")

    def test_list_members_none_without_list_tool(self) -> None:
        runner, calls = self._runner(0, stdout="{}")
        tool = UnarArchiveTool("p7zip", runner=runner)  # no derived lsar

        self.assertIsNone(tool.list_members(Path("/data/rel/movie.rar")))
        self.assertEqual(calls, [])  # never shelled out

    def test_list_members_none_on_bad_json(self) -> None:
        runner, _ = self._runner(0, stdout="not-json{{")
        tool = UnarArchiveTool("unar", list_tool=sys.executable, runner=runner)

        self.assertIsNone(tool.list_members(Path("/data/rel/movie.rar")))

    def test_list_members_none_on_nonzero_exit(self) -> None:
        runner, _ = self._runner(1, stdout="{}")
        tool = UnarArchiveTool("unar", list_tool=sys.executable, runner=runner)

        self.assertIsNone(tool.list_members(Path("/data/rel/movie.rar")))


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture_generator():
    """Import the committed stdlib RAR4 generator by path (tests/ is not a package)."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_rar_fixture", _FIXTURES_DIR / "make_rar_fixture.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("unar"), "unar not installed")
class RealBinaryTests(unittest.TestCase):
    """Drive the real ``unar``/``lsar`` binaries against the committed fixture (#39)."""

    def _committed_archive(self, dest_dir: Path, name: str, build) -> Path:
        """Copy the committed fixture ``name`` into ``dest_dir`` (regenerate if absent).

        ``build`` takes the generator module and returns the archive bytes — the
        fallback never shells out to a `rar` creator.
        """

        committed = _FIXTURES_DIR / name
        archive = dest_dir / name
        if committed.exists():
            shutil.copyfile(committed, archive)
        else:
            archive.write_bytes(build(_load_fixture_generator()))
        return archive

    def _fixture_archive(self, dest_dir: Path) -> Path:
        """The committed single-member ``hello.rar``."""

        return self._committed_archive(
            dest_dir, "hello.rar", lambda gen: gen.build_rar4(gen.FIXTURE_MEMBER, gen.FIXTURE_CONTENT)
        )

    def _nested_fixture_archive(self, dest_dir: Path) -> Path:
        """The committed nested-member ``nested.rar``."""

        return self._committed_archive(
            dest_dir, "nested.rar", lambda gen: gen.build_rar4_multi(gen.NESTED_MEMBERS)
        )

    def test_real_unar_extraction_roundtrip(self) -> None:
        with _Fixture() as fx:
            rel = fx.watch_root / "release"
            rel.mkdir()
            self._fixture_archive(rel)

            extractor = Extractor(
                fx.config(extract_min_age_seconds=0),
                clock=lambda: (rel / "hello.rar").stat().st_mtime + 10_000,
            )
            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            payload = rel / "hello.txt"
            self.assertTrue(payload.exists())
            self.assertEqual(payload.read_text(), "hello from a committed rar fixture\n")

    @unittest.skipUnless(shutil.which("lsar"), "lsar not installed")
    def test_real_lsar_lists_members(self) -> None:
        # Validate the lsar -json parsing against the actual binary, not a fake.
        # Gated on `lsar` specifically: member enumeration derives lsar from unar,
        # and a host with unar but no lsar would fail (list_members → None) rather
        # than skip. (The two normally ship in one package.)
        with _Fixture() as fx:
            archive = self._fixture_archive(fx.watch_root)
            members = UnarArchiveTool("unar").list_members(archive)
            self.assertEqual(members, [Path("hello.txt")])

    @unittest.skipUnless(shutil.which("lsar"), "lsar not installed")
    def test_real_lsar_normalizes_nested_member_separator(self) -> None:
        # The linchpin for #54: the fixture archives the member as `sub\deep.txt`,
        # and lsar reports it as `sub/deep.txt` — the exact relative path unar
        # extracts it to. So `_safe_member_path` maps nested members by plain
        # concatenation, with no platform-specific reconstruction to get wrong.
        gen = _load_fixture_generator()
        with _Fixture() as fx:
            archive = self._nested_fixture_archive(fx.watch_root)
            members = UnarArchiveTool("unar").list_members(archive)
            self.assertEqual(
                members, [Path(gen.NESTED_ROOT_MEMBER), Path(gen.NESTED_DEEP_EXTRACTED)]
            )
            # The archived spelling is the backslash form; lsar normalized it.
            self.assertNotEqual(gen.NESTED_DEEP_MEMBER, gen.NESTED_DEEP_EXTRACTED)

    @unittest.skipUnless(shutil.which("lsar"), "lsar not installed")
    def test_real_unar_nested_byte_identical_overwrite_is_recorded(self) -> None:
        # #54's acceptance case on the real binaries. `unar` restores each member's
        # archived timestamp, so re-extracting rewrites identical bytes with an
        # identical mtime — invisible to the (mtime, size) diff. The member list must
        # still record the *nested* output so the planner protects it.
        # Gated on `lsar` for the same reason as the listing tests above: it is what
        # makes these outputs visible at all, so a host with unar but no lsar would
        # fail on the assertions below rather than skip.
        gen = _load_fixture_generator()
        with _Fixture() as fx:
            rel = fx.watch_root / "release"
            rel.mkdir()
            archive = self._nested_fixture_archive(rel)
            clock = lambda: archive.stat().st_mtime + 10_000  # noqa: E731 - past the settle guard

            # First pass materializes the tree (no ledger ⇒ no output bookkeeping).
            first = Extractor(fx.config(), clock=clock)
            self.assertEqual(
                [r.status for r in first.extract_all((fx.watch_root,), dry_run=False)],
                ["extracted"],
            )
            deep = normalize_path(rel / gen.NESTED_DEEP_EXTRACTED)
            self.assertTrue(deep.exists())  # real unar rebuilt the tree from `sub\deep.txt`
            before = os.stat(deep)

            # Second pass: a fresh ledger re-claims the archive, so it extracts again
            # over the byte-identical files and records what it produced.
            store = StateStore(fx.config().state_db_path)
            second = Extractor(fx.config(), ledger=StateExtractionLedger(store), clock=clock)
            results = second.extract_all((fx.watch_root,), dry_run=False)

            after = os.stat(deep)
            # The fingerprint is genuinely unchanged: the diff alone is blind here.
            self.assertEqual((before.st_mtime, before.st_size), (after.st_mtime, after.st_size))
            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertIn(deep, results[0].outputs)  # caught only by the member list
            self.assertIn(deep, store.get_protected_extracted_paths(0.0, protect_seconds=10**9))
            # no regression to the root-level detection #43 shipped
            self.assertIn(normalize_path(rel / gen.NESTED_ROOT_MEMBER), results[0].outputs)


if __name__ == "__main__":
    unittest.main()
