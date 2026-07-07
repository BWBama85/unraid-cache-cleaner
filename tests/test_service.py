"""Service integration tests."""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.config import Config
from unraid_cache_cleaner.extractor import Extractor
from unraid_cache_cleaner.models import TorrentRecord
from unraid_cache_cleaner.planner import normalize_path
from unraid_cache_cleaner.service import CleanerService
from unraid_cache_cleaner.state import StateExtractionLedger, StateStore


class FakeClient:
    def __init__(self, torrents: list[TorrentRecord], default_save_path: Path) -> None:
        self._torrents = torrents
        self._default_save_path = default_save_path

    def fetch_torrents(self) -> list[TorrentRecord]:
        return list(self._torrents)

    def fetch_default_save_path(self) -> Path:
        return self._default_save_path


class FakeExtractTool:
    """Injected archive tool that writes a `.mkv` on extract; records calls."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.extract_calls: list[Path] = []

    def is_available(self) -> bool:
        return self.available

    def test(self, archive: Path) -> bool:
        return True

    def extract(self, archive: Path, dest_dir: Path) -> None:
        self.extract_calls.append(archive)
        (dest_dir / (Path(archive.name).stem + ".mkv")).write_text("extracted")


def _config(watch_root: Path, config_root: Path, **overrides: object) -> Config:
    base: dict[str, object] = dict(
        qbittorrent_url="http://qbt:8080",
        qbittorrent_username="admin",
        qbittorrent_password="secret",
        qbittorrent_timeout_seconds=15,
        qbittorrent_verify_tls=True,
        watch_paths=(watch_root,),
        poll_interval_seconds=300,
        orphan_grace_seconds=0,
        min_file_age_seconds=0,
        dry_run=False,
        delete_empty_dirs=True,
        protect_single_file_parent_dirs=True,
        excluded_globs=(),
        state_db_path=config_root / "state.sqlite3",
        report_path=config_root / "last-run.json",
        log_level="INFO",
        plex_duplicate_report_path=config_root / "plex-duplicates.json",
        extract_min_age_seconds=0,
    )
    base.update(overrides)
    config = Config(**base)  # type: ignore[arg-type]
    config.ensure_directories()
    return config


class ExtractionServiceTests(unittest.TestCase):
    """Extraction folded into run_once, reconciled with the deletion path (#35/#36)."""

    def _service(self, config: Config, torrents: list[TorrentRecord], watch_root: Path, tool):
        store = StateStore(config.state_db_path)
        extractor = Extractor(config, tool=tool, ledger=StateExtractionLedger(store))
        return CleanerService(config, FakeClient(torrents, watch_root), store, extractor=extractor)

    def test_extracted_output_is_not_deleted_in_the_same_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root, config_root = root / "data", root / "config"
            job = watch_root / "job"
            job.mkdir(parents=True)
            config_root.mkdir()
            (job / "release.rar").write_text("rar")
            (watch_root / "orphan.txt").write_text("orphan")

            # Single-file torrent, parent protection OFF: the extracted .mkv is only
            # safe because extraction injects it into this cycle's protection.
            torrent = TorrentRecord(
                torrent_hash="abc",
                name="release.rar",
                state="uploading",
                save_path=watch_root,
                content_path=job / "release.rar",
                progress=1.0,
            )
            config = _config(
                watch_root, config_root,
                extract_enabled=True,
                protect_single_file_parent_dirs=False,
            )
            tool = FakeExtractTool()
            service = self._service(config, [torrent], watch_root, tool)

            report = service.run_once()

            self.assertEqual(len(tool.extract_calls), 1)
            self.assertTrue((job / "release.mkv").exists())  # extracted media kept
            self.assertFalse((watch_root / "orphan.txt").exists())  # cleanup still ran
            statuses = {(a.action, a.status) for a in report.actions}
            self.assertIn(("extract", "extracted"), statuses)

    def test_disabled_never_invokes_the_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root, config_root = root / "data", root / "config"
            watch_root.mkdir(parents=True)
            config_root.mkdir()
            (watch_root / "movie.rar").write_text("rar")

            config = _config(watch_root, config_root, extract_enabled=False)
            tool = FakeExtractTool()
            service = self._service(config, [], watch_root, tool)

            report = service.run_once()

            self.assertEqual(tool.extract_calls, [])
            self.assertFalse(any(a.action == "extract" for a in report.actions))

    def test_missing_binary_logs_and_cleanup_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root, config_root = root / "data", root / "config"
            watch_root.mkdir(parents=True)
            config_root.mkdir()
            (watch_root / "orphan.txt").write_text("orphan")

            config = _config(watch_root, config_root, extract_enabled=True)
            service = self._service(config, [], watch_root, FakeExtractTool(available=False))

            report = service.run_once()  # must not raise

            self.assertFalse(any(a.action == "extract" for a in report.actions))
            self.assertFalse((watch_root / "orphan.txt").exists())  # deletion still ran

    def test_dry_run_reports_would_extract_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root, config_root = root / "data", root / "config"
            job = watch_root / "job"
            job.mkdir(parents=True)
            config_root.mkdir()
            (job / "release.rar").write_text("rar")

            config = _config(watch_root, config_root, extract_enabled=True, dry_run=True)
            tool = FakeExtractTool()
            store = StateStore(config.state_db_path)
            extractor = Extractor(config, tool=tool, ledger=StateExtractionLedger(store))
            service = CleanerService(config, FakeClient([], watch_root), store, extractor=extractor)

            report = service.run_once()

            self.assertEqual(tool.extract_calls, [])
            self.assertFalse((job / "release.mkv").exists())
            statuses = {(a.action, a.status) for a in report.actions}
            self.assertIn(("extract", "would_extract"), statuses)
            self.assertEqual(store.get_protected_extracted_paths(0.0, protect_seconds=10**9), set())


class ServiceTests(unittest.TestCase):
    def test_dry_run_reports_eligible_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root = root / "data"
            config_root = root / "config"
            watch_root.mkdir()
            config_root.mkdir()
            orphan = watch_root / "orphan.mkv"
            orphan.write_text("orphan")

            config = Config(
                qbittorrent_url="http://qbt:8080",
                qbittorrent_username="admin",
                qbittorrent_password="secret",
                qbittorrent_timeout_seconds=15,
                qbittorrent_verify_tls=True,
                watch_paths=(watch_root,),
                poll_interval_seconds=300,
                orphan_grace_seconds=0,
                min_file_age_seconds=0,
                dry_run=True,
                delete_empty_dirs=True,
                protect_single_file_parent_dirs=True,
                excluded_globs=(),
                state_db_path=config_root / "state.sqlite3",
                report_path=config_root / "last-run.json",
                log_level="INFO",
                plex_duplicate_report_path=config_root / "plex-duplicates.json",
            )
            config.ensure_directories()
            service = CleanerService(config, FakeClient([], watch_root), StateStore(config.state_db_path))

            report = service.run_once()

            self.assertTrue(orphan.exists())
            self.assertEqual(report.eligible_count, 1)
            self.assertEqual(report.actions[0].status, "would_delete")

    def test_deletes_orphans_and_removes_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root = root / "data"
            config_root = root / "config"
            active_dir = watch_root / "active-release"
            orphan_dir = watch_root / "old-release"
            active_dir.mkdir(parents=True)
            orphan_dir.mkdir(parents=True)
            config_root.mkdir()

            active_file = active_dir / "release.rar"
            orphan_file = orphan_dir / "release.mkv"
            active_file.write_text("tracked")
            orphan_file.write_text("orphan")

            torrent = TorrentRecord(
                torrent_hash="abc",
                name="active-release",
                state="uploading",
                save_path=watch_root,
                content_path=active_dir,
            )
            config = Config(
                qbittorrent_url="http://qbt:8080",
                qbittorrent_username="admin",
                qbittorrent_password="secret",
                qbittorrent_timeout_seconds=15,
                qbittorrent_verify_tls=True,
                watch_paths=(watch_root,),
                poll_interval_seconds=300,
                orphan_grace_seconds=0,
                min_file_age_seconds=0,
                dry_run=False,
                delete_empty_dirs=True,
                protect_single_file_parent_dirs=True,
                excluded_globs=(),
                state_db_path=config_root / "state.sqlite3",
                report_path=config_root / "last-run.json",
                log_level="INFO",
                plex_duplicate_report_path=config_root / "plex-duplicates.json",
            )
            config.ensure_directories()
            service = CleanerService(config, FakeClient([torrent], watch_root), StateStore(config.state_db_path))

            report = service.run_once()

            self.assertFalse(orphan_file.exists())
            self.assertFalse(orphan_dir.exists())
            self.assertTrue(active_file.exists())
            self.assertEqual({action.action for action in report.actions}, {"delete", "rmdir"})

    def test_excluded_globs_keep_non_torrent_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_root = root / "data"
            config_root = root / "config"
            logs_dir = watch_root / "logs"
            logs_dir.mkdir(parents=True)
            config_root.mkdir()

            excluded_file = logs_dir / "script.log"
            orphan_file = watch_root / "orphan.mkv"
            excluded_file.write_text("keep")
            orphan_file.write_text("delete")

            config = Config(
                qbittorrent_url="http://qbt:8080",
                qbittorrent_username="admin",
                qbittorrent_password="secret",
                qbittorrent_timeout_seconds=15,
                qbittorrent_verify_tls=True,
                watch_paths=(watch_root,),
                poll_interval_seconds=300,
                orphan_grace_seconds=0,
                min_file_age_seconds=0,
                dry_run=True,
                delete_empty_dirs=True,
                protect_single_file_parent_dirs=True,
                excluded_globs=("script.log",),
                state_db_path=config_root / "state.sqlite3",
                report_path=config_root / "last-run.json",
                log_level="INFO",
                plex_duplicate_report_path=config_root / "plex-duplicates.json",
            )
            config.ensure_directories()
            service = CleanerService(config, FakeClient([], watch_root), StateStore(config.state_db_path))

            report = service.run_once()

            self.assertEqual(report.eligible_count, 1)
            self.assertEqual(report.actions[0].path, normalize_path(orphan_file))
            self.assertNotEqual(report.actions[0].path, normalize_path(excluded_file))


if __name__ == "__main__":
    unittest.main()
