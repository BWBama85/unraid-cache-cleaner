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
from unraid_cache_cleaner.models import PlexSection
from unraid_cache_cleaner.plex import PlexClientError

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


if __name__ == "__main__":
    unittest.main()
