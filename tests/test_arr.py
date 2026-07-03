"""Tests for the Radarr/Sonarr clients and the pure association engine."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import arr, dedupe
from unraid_cache_cleaner.arr import (
    ArrClientError,
    RadarrClient,
    SonarrClient,
    annotate,
)
from unraid_cache_cleaner.models import DuplicateGroup, MediaCopy

GiB = 1024 ** 3


# --------------------------------------------------------------------------- #
# Fake urllib transport (mirrors tests/test_plex.py)                           #
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RecordingOpener:
    """Records requests, returns canned bodies keyed off each request."""

    def __init__(self, responder) -> None:
        self.requests = []
        self._responder = responder

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _FakeResponse(self._responder(request))


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://arr:7878", code, "err", {}, io.BytesIO(body))


def _raiser(exc: Exception):
    def responder(_request):
        raise exc

    return responder


def _radarr(opener) -> RadarrClient:
    client = RadarrClient("http://radarr:7878", "SECRET-KEY-123")
    client._opener = opener
    return client


def _sonarr(opener) -> SonarrClient:
    client = SonarrClient("http://sonarr:8989", "SECRET-KEY-123")
    client._opener = opener
    return client


# --------------------------------------------------------------------------- #
# Client tests                                                                 #
# --------------------------------------------------------------------------- #

class ArrClientTests(unittest.TestCase):
    def test_requires_url_and_key(self) -> None:
        with self.assertRaises(ArrClientError):
            RadarrClient("", "key")
        with self.assertRaises(ArrClientError):
            RadarrClient("http://radarr:7878", "")

    def test_api_key_is_header_not_url(self) -> None:
        client = RadarrClient("http://radarr:7878", "SECRET-KEY-123")
        self.assertIn(("X-Api-Key", "SECRET-KEY-123"), client._opener.addheaders)
        self.assertIn(("Accept", "application/json"), client._opener.addheaders)

    def test_radarr_index_maps_tmdb_to_basenames(self) -> None:
        movies = [
            {"tmdbId": 111, "movieFile": {"path": "/movies/A (2020)/a.4k.mkv"}},
            {"tmdbId": 222, "movieFile": {"relativePath": "b.1080.mkv"}},  # path fallback
            {"tmdbId": 333, "hasFile": False},  # not imported -> skipped
            {"movieFile": {"path": "/x/no-id.mkv"}},  # no tmdbId -> skipped
        ]
        opener = _RecordingOpener(lambda req: json.dumps(movies).encode("utf-8"))
        client = _radarr(opener)

        index = client.fetch_tracked_index()

        self.assertEqual(index, {"111": {"a.4k.mkv"}, "222": {"b.1080.mkv"}})
        self.assertEqual(urlparse(opener.requests[0].full_url).path, "/api/v3/movie")

    def test_sonarr_index_collects_episode_basenames(self) -> None:
        series = [{"id": 1, "tvdbId": 9001}, {"id": 2, "tvdbId": 9002}]
        files_by_series = {
            "1": [{"path": "/tv/Show A/S01/a1.mkv"}, {"path": "/tv/Show A/S01/a2.mkv"}],
            "2": [{"path": "/tv/Show B/S01/b1.mkv"}, {"relativePath": "no-path.mkv"}],
        }

        def responder(req) -> bytes:
            path = urlparse(req.full_url).path
            if path == "/api/v3/series":
                return json.dumps(series).encode("utf-8")
            series_id = parse_qs(urlparse(req.full_url).query)["seriesId"][0]
            return json.dumps(files_by_series[series_id]).encode("utf-8")

        opener = _RecordingOpener(responder)
        client = _sonarr(opener)

        basenames = client.fetch_tracked_index()

        self.assertEqual(basenames, {"a1.mkv", "a2.mkv", "b1.mkv"})
        # one /series call plus one /episodefile call per series
        paths = [urlparse(r.full_url).path for r in opener.requests]
        self.assertEqual(paths.count("/api/v3/episodefile"), 2)

    def test_401_raises_and_no_key_leak(self) -> None:
        opener = _RecordingOpener(_raiser(_http_error(401, b"denied")))
        client = _radarr(opener)

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))

    def test_connection_failure_no_key_leak(self) -> None:
        opener = _RecordingOpener(_raiser(urllib.error.URLError("refused")))
        client = _radarr(opener)

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()

        message = str(ctx.exception)
        self.assertIn("Unable to connect to Radarr at http://radarr:7878", message)
        self.assertNotIn("SECRET-KEY-123", message)
        self.assertNotIn("SECRET-KEY-123", opener.requests[0].full_url)

    def test_http_500_carries_status_no_key_leak(self) -> None:
        opener = _RecordingOpener(_raiser(_http_error(500, b"boom")))
        client = _sonarr(opener)

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))


# --------------------------------------------------------------------------- #
# annotate() — pure association engine                                         #
# --------------------------------------------------------------------------- #

def _movie_group(tmdb, files):
    """files: list of (path, size, resolution). Each is its own logical copy."""

    copies = tuple(
        MediaCopy(
            part_id=i,
            file=Path(path),
            size=size,
            resolution=res,
            bitrate=9000,
            media_id=i + 1,
        )
        for i, (path, size, res) in enumerate(files)
    )
    group = DuplicateGroup(
        rating_key="1",
        kind="movie",
        title="Movie",
        copies=copies,
        external_ids={"tmdb": tmdb} if tmdb else {},
    )
    return dedupe.analyze_group(group)


def _episode_group(files):
    copies = tuple(
        MediaCopy(part_id=i, file=Path(path), size=size, resolution=res, bitrate=9000, media_id=i + 1)
        for i, (path, size, res) in enumerate(files)
    )
    group = DuplicateGroup(
        rating_key="9",
        kind="episode",
        title="Show - S01E01",
        copies=copies,
        external_ids={"tvdb": "555"},
    )
    return dedupe.analyze_group(group)


def _by_name(group):
    return {c.file.name: (c.association, c.arr_tracked) for c in group.copies}


class AnnotateMovieTests(unittest.TestCase):
    def test_tracked_keeper_and_untracked_sibling(self) -> None:
        group = _movie_group(
            "123",
            [("/movies/M/big.4k.mkv", 20 * GiB, "4k"), ("/movies/M/big.1080.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"big.4k.mkv"}}, set())

        self.assertEqual(
            _by_name(out),
            {"big.4k.mkv": (arr.TRACKED, arr.RADARR), "big.1080.mkv": (arr.UNTRACKED, None)},
        )
        # keeper (the 4k copy) carries the tracked association, not the default
        self.assertEqual(out.keeper.association, arr.TRACKED)
        self.assertEqual(out.keeper.arr_tracked, arr.RADARR)
        # classification + reclaimable math are unchanged by annotation
        self.assertEqual(out.classification, group.classification)
        self.assertEqual(out.reclaimable_bytes, group.reclaimable_bytes)

    def test_id_matches_but_no_basename_match_is_unknown(self) -> None:
        group = _movie_group(
            "123",
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"somewhere-else.mkv"}}, set())

        self.assertEqual(
            {c.association for c in out.copies}, {arr.UNKNOWN}
        )
        self.assertTrue(all(c.arr_tracked is None for c in out.copies))

    def test_no_id_match_is_unknown(self) -> None:
        group = _movie_group(
            "999",
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"a.mkv"}}, set())
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_missing_plex_id_is_unknown(self) -> None:
        group = _movie_group(
            None,
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"a.mkv"}}, set())
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_empty_radarr_index_leaves_movies_unknown(self) -> None:
        group = _movie_group(
            "123",
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {}, set())
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_stacked_parts_preserve_first_part_association(self) -> None:
        # A stacked copy (cd1+cd2 share media_id) plus a second single copy.
        cd1 = MediaCopy(part_id=1, file=Path("/m/cd1.mkv"), size=5 * GiB, resolution="1080", media_id=10)
        cd2 = MediaCopy(part_id=2, file=Path("/m/cd2.mkv"), size=5 * GiB, resolution="1080", media_id=10)
        other = MediaCopy(part_id=3, file=Path("/m/other.mkv"), size=6 * GiB, resolution="1080", media_id=11)
        group = dedupe.analyze_group(
            DuplicateGroup(
                rating_key="1",
                kind="movie",
                title="Stacked",
                copies=(cd1, cd2, other),
                external_ids={"tmdb": "123"},
            )
        )

        [out] = annotate([group], {"123": {"cd1.mkv"}}, set())

        # the merged logical copy (keyed on cd1) is tracked; the other is untracked
        ranked = {c.file.name: c.association for c in dedupe.rank_copies(out)}
        self.assertEqual(ranked["cd1.mkv"], arr.TRACKED)
        self.assertEqual(ranked["other.mkv"], arr.UNTRACKED)


class AnnotateEpisodeTests(unittest.TestCase):
    def test_tracked_by_basename_extra_is_unknown_not_untracked(self) -> None:
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e1.720.mkv", 2 * GiB, "720")],
        )
        [out] = annotate([group], {}, {"e1.mkv"})

        self.assertEqual(
            _by_name(out),
            {"e1.mkv": (arr.TRACKED, arr.SONARR), "e1.720.mkv": (arr.UNKNOWN, None)},
        )

    def test_no_basename_match_all_unknown(self) -> None:
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e2.mkv", 3 * GiB, "1080")],
        )
        [out] = annotate([group], {}, {"totally-different.mkv"})
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_other_kind_passes_through_untouched(self) -> None:
        copies = (
            MediaCopy(part_id=1, file=Path("/x/a"), size=GiB, media_id=1),
            MediaCopy(part_id=2, file=Path("/x/b"), size=GiB, media_id=2),
        )
        group = dedupe.analyze_group(
            DuplicateGroup(rating_key="1", kind="other", title="X", copies=copies)
        )
        [out] = annotate([group], {"1": {"a"}}, {"a"})
        self.assertIs(out, group)


if __name__ == "__main__":
    unittest.main()
