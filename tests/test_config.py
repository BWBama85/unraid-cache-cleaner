"""Configuration parsing tests."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.config import (
    DEFAULT_EXCLUDED_GLOBS,
    Config,
    _parse_glob_list,
    _parse_str_list,
)


class ConfigTests(unittest.TestCase):
    def test_excluded_globs_default_when_unset(self) -> None:
        self.assertEqual(_parse_glob_list(None), DEFAULT_EXCLUDED_GLOBS)
        self.assertEqual(_parse_glob_list(""), DEFAULT_EXCLUDED_GLOBS)

    def test_parse_str_list(self) -> None:
        self.assertEqual(_parse_str_list(None), ())
        self.assertEqual(_parse_str_list("  "), ())
        self.assertEqual(_parse_str_list("1, 2 ,,3"), ("1", "2", "3"))

    def test_excluded_globs_merge_with_defaults(self) -> None:
        result = _parse_glob_list("*.nfo, *.part , keep.log")
        # defaults come first, in order
        self.assertEqual(result[: len(DEFAULT_EXCLUDED_GLOBS)], DEFAULT_EXCLUDED_GLOBS)
        # user globs are appended
        self.assertIn("*.nfo", result)
        self.assertIn("keep.log", result)
        # a user glob that is already a default is not duplicated
        self.assertEqual(result.count("*.part"), 1)

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
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertEqual(config.qbittorrent_url, "http://qbt:8080")
                self.assertEqual(config.watch_paths, (Path("/data"), Path("/downloads/media")))
                self.assertFalse(config.dry_run)
                self.assertTrue(config.delete_empty_dirs)
                self.assertTrue(config.state_db_path.parent.exists())
                self.assertTrue(config.report_path.parent.exists())
                self.assertTrue(config.plex_duplicate_report_path.parent.exists())

    def test_plex_defaults_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertEqual(config.plex_url, "")
                self.assertEqual(config.plex_token, "")
                self.assertEqual(config.plex_sections, ())
                self.assertEqual(config.plex_timeout_seconds, 30)
                self.assertTrue(config.plex_verify_tls)

    def test_plex_env_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
                "PLEX_URL": "http://plex:32400",
                "PLEX_TOKEN": "abc123",
                "PLEX_SECTIONS": "1, 2 ,3",
                "PLEX_TIMEOUT_SECONDS": "45",
                "PLEX_VERIFY_TLS": "false",
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertEqual(config.plex_url, "http://plex:32400")
                self.assertEqual(config.plex_token, "abc123")
                self.assertEqual(config.plex_sections, ("1", "2", "3"))
                self.assertEqual(config.plex_timeout_seconds, 45)
                self.assertFalse(config.plex_verify_tls)
                self.assertEqual(
                    config.plex_duplicate_report_path,
                    Path(tempdir) / "plex" / "dupes.json",
                )
                self.assertTrue(config.plex_duplicate_report_path.parent.exists())

    def test_arr_defaults_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertEqual(config.radarr_url, "")
                self.assertEqual(config.radarr_api_key, "")
                self.assertEqual(config.radarr_timeout_seconds, 30)
                self.assertTrue(config.radarr_verify_tls)
                self.assertEqual(config.sonarr_url, "")
                self.assertEqual(config.sonarr_api_key, "")
                self.assertEqual(config.sonarr_timeout_seconds, 30)
                self.assertTrue(config.sonarr_verify_tls)

    def test_arr_env_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
                "RADARR_URL": "http://radarr:7878",
                "RADARR_API_KEY": "rkey",
                "RADARR_TIMEOUT_SECONDS": "20",
                "RADARR_VERIFY_TLS": "false",
                "SONARR_URL": "http://sonarr:8989",
                "SONARR_API_KEY": "skey",
                "SONARR_TIMEOUT_SECONDS": "25",
                "SONARR_VERIFY_TLS": "false",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertEqual(config.radarr_url, "http://radarr:7878")
                self.assertEqual(config.radarr_api_key, "rkey")
                self.assertEqual(config.radarr_timeout_seconds, 20)
                self.assertFalse(config.radarr_verify_tls)
                self.assertEqual(config.sonarr_url, "http://sonarr:8989")
                self.assertEqual(config.sonarr_api_key, "skey")
                self.assertEqual(config.sonarr_timeout_seconds, 25)
                self.assertFalse(config.sonarr_verify_tls)

    def test_extract_defaults_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                # Extraction mutates the download path, so it is off by default.
                self.assertFalse(config.extract_enabled)
                self.assertEqual(config.extract_tool, "unar")
                self.assertEqual(config.extract_owner, "")
                self.assertEqual(config.extract_min_age_seconds, 300)
                self.assertEqual(config.extract_protect_seconds, 86400)

    def test_extract_env_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "STATE_DB_PATH": str(Path(tempdir) / "state" / "db.sqlite3"),
                "REPORT_PATH": str(Path(tempdir) / "reports" / "last-run.json"),
                "PLEX_DUPLICATE_REPORT_PATH": str(Path(tempdir) / "plex" / "dupes.json"),
                "EXTRACT_ENABLED": "true",
                "EXTRACT_TOOL": "/usr/bin/unar",
                "EXTRACT_OWNER": "99:100",
                "EXTRACT_MIN_AGE_SECONDS": "60",
                "EXTRACT_PROTECT_SECONDS": "3600",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

                self.assertTrue(config.extract_enabled)
                self.assertEqual(config.extract_tool, "/usr/bin/unar")
                self.assertEqual(config.extract_owner, "99:100")
                self.assertEqual(config.extract_min_age_seconds, 60)
                self.assertEqual(config.extract_protect_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
