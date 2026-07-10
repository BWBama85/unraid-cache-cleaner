"""CLI dispatch tests for the plex-duplicates subcommand."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import cli
from unraid_cache_cleaner.arr import ArrClientError
from unraid_cache_cleaner.models import PlexSection
from unraid_cache_cleaner.plex import PlexClientError
from unraid_cache_cleaner.web_actions import StagingSweepReport

GiB = 1024 ** 3

_MOVIE = {
    "ratingKey": "100",
    "type": "movie",
    "title": "Big Movie",
    "year": 2020,
    "Media": [
        {"id": 1, "videoResolution": "4k", "bitrate": 20000,
         "Part": [{"id": 11, "file": "/movies/big.4k.mkv", "size": 20 * GiB}]},
        {"id": 2, "videoResolution": "1080", "bitrate": 9000,
         "Part": [{"id": 12, "file": "/movies/big.1080.mkv", "size": 8 * GiB}]},
    ],
}


class FakePlexClient:
    def __init__(self, *, sections=None, duplicates=None, error=None) -> None:
        self._sections = sections or [PlexSection(key="1", type="movie", title="Movies")]
        self._duplicates = duplicates or {("1", 1): [_MOVIE]}
        self._error = error

    def fetch_sections(self):
        if self._error is not None:
            raise self._error
        return list(self._sections)

    def fetch_duplicates(self, section_id, item_type, *, page_size=200):
        return list(self._duplicates.get((section_id, item_type), []))


@contextlib.contextmanager
def _env(tmp: Path):
    overrides = {
        "PLEX_URL": "http://plex:32400",
        "PLEX_TOKEN": "TOKEN",
        "PLEX_SECTIONS": "",
        "STATE_DB_PATH": str(tmp / "state.sqlite3"),
        "REPORT_PATH": str(tmp / "last-run.json"),
        "PLEX_DUPLICATE_REPORT_PATH": str(tmp / "plex-duplicates.json"),
    }
    with mock.patch.dict(os.environ, overrides, clear=False):
        yield


def _run(argv, fake):
    """Run cli.main(argv) with PlexClient/Qbittorrent/StateStore patched."""

    out = io.StringIO()
    with mock.patch.object(cli, "PlexClient", return_value=fake), \
            mock.patch.object(cli, "QbittorrentClient") as qbt, \
            mock.patch.object(cli, "StateStore") as store, \
            contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue(), qbt, store


class PlexDuplicatesCliTests(unittest.TestCase):
    def test_success_writes_report_and_prints_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _env(Path(tmpdir)):
            tmp = Path(tmpdir)
            code, stdout, qbt, store = _run(["plex-duplicates"], FakePlexClient())

            self.assertEqual(code, 0)
            self.assertIn("Reclaimable (safe)", stdout)
            payload = json.loads((tmp / "plex-duplicates.json").read_text())
            self.assertEqual(payload["totals"]["reclaimable_bytes"], 8 * GiB)
            # a Plex report must not touch the qBittorrent client or state DB
            qbt.assert_not_called()
            store.assert_not_called()

    def test_json_only_suppresses_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _env(Path(tmpdir)):
            tmp = Path(tmpdir)
            code, stdout, _, _ = _run(["plex-duplicates", "--json-only"], FakePlexClient())

            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertTrue((tmp / "plex-duplicates.json").exists())

    def test_plex_client_error_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _env(Path(tmpdir)):
            fake = FakePlexClient(error=PlexClientError("boom", status_code=401))
            code, _, _, _ = _run(["plex-duplicates"], fake)
            self.assertEqual(code, 2)

    def test_generic_runtime_error_returns_3(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _env(Path(tmpdir)):
            fake = FakePlexClient(error=RuntimeError("unexpected"))
            code, _, _, _ = _run(["plex-duplicates"], fake)
            self.assertEqual(code, 3)


class _FakeArr:
    def __init__(self, index, *, raises=None) -> None:
        self._index = index
        self._raises = raises

    def fetch_tracked_index(self):
        if self._raises is not None:
            raise self._raises
        return type(self._index)(self._index)


class ArrCliTests(unittest.TestCase):
    def test_radarr_configured_enables_association(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _env(Path(tmpdir)):
            tmp = Path(tmpdir)
            arr_env = {"RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "rkey"}
            out = io.StringIO()
            with mock.patch.dict(os.environ, arr_env, clear=False), \
                    mock.patch.object(cli, "PlexClient", return_value=FakePlexClient()), \
                    mock.patch.object(cli, "RadarrClient", return_value=_FakeArr({"1": {"x"}})) as radarr, \
                    mock.patch.object(cli, "QbittorrentClient"), \
                    mock.patch.object(cli, "StateStore"), \
                    contextlib.redirect_stdout(out):
                code = cli.main(["plex-duplicates", "--json-only"])

            self.assertEqual(code, 0)
            # client built from config (url, key, and the per-service knobs)
            radarr.assert_called_once()
            payload = json.loads((tmp / "plex-duplicates.json").read_text())
            self.assertTrue(payload["arr_enabled"])

    def test_arr_outage_degrades_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _env(Path(tmpdir)):
            tmp = Path(tmpdir)
            arr_env = {"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "skey"}
            fake = _FakeArr(set(), raises=ArrClientError("down", status_code=500))
            with mock.patch.dict(os.environ, arr_env, clear=False), \
                    mock.patch.object(cli, "PlexClient", return_value=FakePlexClient()), \
                    mock.patch.object(cli, "SonarrClient", return_value=fake), \
                    mock.patch.object(cli, "QbittorrentClient"), \
                    mock.patch.object(cli, "StateStore"), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["plex-duplicates", "--json-only"])

            # a *arr outage never fails the read-only report
            self.assertEqual(code, 0)
            payload = json.loads((tmp / "plex-duplicates.json").read_text())
            self.assertTrue(payload["arr_enabled"])
            self.assertTrue(any("Sonarr association skipped" in w for w in payload["warnings"]))


@contextlib.contextmanager
def _extract_env(tmp: Path, **overrides):
    env = {
        "WATCH_PATHS": str(tmp / "data"),
        "STATE_DB_PATH": str(tmp / "state.sqlite3"),
        "REPORT_PATH": str(tmp / "last-run.json"),
        "PLEX_DUPLICATE_REPORT_PATH": str(tmp / "plex-duplicates.json"),
    }
    env.update(overrides)
    (tmp / "data").mkdir(exist_ok=True)
    with mock.patch.dict(os.environ, env, clear=True):
        yield


class ExtractCliTests(unittest.TestCase):
    def test_disabled_is_noop_and_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _extract_env(Path(tmpdir)):
            with mock.patch.object(cli, "Extractor") as extractor, \
                    mock.patch.object(cli, "QbittorrentClient") as qbt, \
                    mock.patch.object(cli, "StateStore") as store:
                code = cli.main(["extract"])

            self.assertEqual(code, 0)
            extractor.assert_not_called()
            qbt.assert_not_called()
            store.assert_not_called()

    def test_enabled_runs_and_shares_ledger_not_qbittorrent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
                _extract_env(Path(tmpdir), EXTRACT_ENABLED="true", DRY_RUN="false"):
            fake = mock.Mock()
            fake.extract_all.return_value = []
            with mock.patch.object(cli, "Extractor", return_value=fake) as extractor, \
                    mock.patch.object(cli, "QbittorrentClient") as qbt, \
                    mock.patch.object(cli, "StateStore") as store:
                code = cli.main(["extract"])

            self.assertEqual(code, 0)
            extractor.assert_called_once()
            _, kwargs = fake.extract_all.call_args
            self.assertFalse(kwargs["dry_run"])
            # the extract command must not build the qBittorrent client, but it
            # shares the SQLite ledger (claim-before-extract, cross-run idempotency)
            qbt.assert_not_called()
            store.assert_called_once()

    def test_honors_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
                _extract_env(Path(tmpdir), EXTRACT_ENABLED="true", DRY_RUN="true"):
            fake = mock.Mock()
            fake.extract_all.return_value = []
            with mock.patch.object(cli, "Extractor", return_value=fake), \
                    mock.patch.object(cli, "QbittorrentClient"), \
                    mock.patch.object(cli, "StateStore"):
                code = cli.main(["extract"])

            self.assertEqual(code, 0)
            _, kwargs = fake.extract_all.call_args
            self.assertTrue(kwargs["dry_run"])

    def test_missing_watch_paths_returns_3(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
                _extract_env(Path(tmpdir), EXTRACT_ENABLED="true", WATCH_PATHS=""):
            with mock.patch.object(cli, "Extractor") as extractor, \
                    mock.patch.object(cli, "QbittorrentClient"), \
                    mock.patch.object(cli, "StateStore"):
                code = cli.main(["extract"])

            self.assertEqual(code, 3)
            extractor.assert_not_called()


class SafePrintTests(unittest.TestCase):
    class _AsciiStdout(io.StringIO):
        """Stand-in for a non-UTF-8 terminal: rejects non-ASCII on write."""

        encoding = "ascii"

        def write(self, s):  # type: ignore[override]
            s.encode("ascii")  # raises UnicodeEncodeError on non-ASCII
            return super().write(s)

    def test_non_ascii_output_does_not_raise(self) -> None:
        out = self._AsciiStdout()
        with mock.patch("sys.stdout", out):
            cli._safe_print("Amélie duplicate report")  # must not raise
        printed = out.getvalue()
        self.assertIn("Am", printed)
        self.assertIn("?", printed)  # é replaced, not crashed


class ReconcileWebStagingTests(unittest.TestCase):
    """#72: the web-startup staging sweep is gated on an enabled action layer + a
    configured media-path map, and is fail-soft (a sweep error never blocks startup)."""

    class _FakeReclaim:
        def __init__(self, *, report=None, exc=None):
            self.calls = 0
            self._report = report if report is not None else StagingSweepReport()
            self._exc = exc

        def reconcile_staging(self):
            self.calls += 1
            if self._exc is not None:
                raise self._exc
            return self._report

    class _Cfg:
        def __init__(self, *, mapped):
            self.web_media_path_map = (
                ((Path("/plex"), Path("/media")),) if mapped else ()
            )

    def test_runs_when_service_present_and_mapped(self) -> None:
        svc = self._FakeReclaim(report=StagingSweepReport(restored=1))
        cli._reconcile_web_staging(svc, self._Cfg(mapped=True))
        self.assertEqual(svc.calls, 1)

    def test_skips_when_actions_disabled(self) -> None:
        # No reclaim service (actions off) — nothing to sweep, and no crash.
        cli._reconcile_web_staging(None, self._Cfg(mapped=True))

    def test_skips_when_no_path_map(self) -> None:
        svc = self._FakeReclaim()
        cli._reconcile_web_staging(svc, self._Cfg(mapped=False))
        self.assertEqual(svc.calls, 0)  # no roots to sweep → never invoked

    def test_sweep_failure_is_swallowed(self) -> None:
        svc = self._FakeReclaim(exc=OSError("boom"))
        # A sweep failure must be logged and swallowed, never propagate to block the
        # read-only viewer from starting.
        cli._reconcile_web_staging(svc, self._Cfg(mapped=True))
        self.assertEqual(svc.calls, 1)


if __name__ == "__main__":
    unittest.main()
