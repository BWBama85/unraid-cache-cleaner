"""Configuration parsing tests."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.config import Config


class ConfigTests(unittest.TestCase):
    def test_from_env_parses_lists_and_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "QBITTORRENT_URL": "http://qbt:8080",
                "QBITTORRENT_USERNAME": "admin",
                "QBITTORRENT_PASSWORD": "secret",
                "WATCH_PATHS": "/data,/downloads/media",
                "DRY_RUN": "false",
                "DELETE_EMPTY_DIRS": "true",
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertEqual(config.qbittorrent_url, "http://qbt:8080")
                self.assertEqual(config.watch_paths, (Path("/data"), Path("/downloads/media")))
                self.assertFalse(config.dry_run)
                self.assertTrue(config.delete_empty_dirs)
                self.assertTrue(config.state_db_path.parent.exists())
                self.assertTrue(config.report_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
