"""Service integration tests."""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.config import Config
from unraid_cache_cleaner.models import TorrentRecord
from unraid_cache_cleaner.service import CleanerService
from unraid_cache_cleaner.state import StateStore


class FakeClient:
    def __init__(self, torrents: list[TorrentRecord], default_save_path: Path) -> None:
        self._torrents = torrents
        self._default_save_path = default_save_path

    def fetch_torrents(self) -> list[TorrentRecord]:
        return list(self._torrents)

    def fetch_default_save_path(self) -> Path:
        return self._default_save_path


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
            )
            config.ensure_directories()
            service = CleanerService(config, FakeClient([torrent], watch_root), StateStore(config.state_db_path))

            report = service.run_once()

            self.assertFalse(orphan_file.exists())
            self.assertFalse(orphan_dir.exists())
            self.assertTrue(active_file.exists())
            self.assertEqual({action.action for action in report.actions}, {"delete", "rmdir"})


if __name__ == "__main__":
    unittest.main()
