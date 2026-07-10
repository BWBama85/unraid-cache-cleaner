"""Tests for the Radarr/Sonarr clients and the pure association engine."""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fake_http import FakeHTTPHandler as _FakeHTTPHandler
from _fake_http import FakeHTTPResponse as _FakeHTTPResponse
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

    def test_radarr_index_maps_tmdb_to_basename_ids(self) -> None:
        movies = [
            {"tmdbId": 111, "movieFile": {"id": 10, "path": "/movies/A (2020)/a.4k.mkv"}},
            {"tmdbId": 222, "movieFile": {"relativePath": "b.1080.mkv", "id": 20}},  # path fallback
            {"tmdbId": 444, "movieFile": {"path": "/movies/D/d.mkv"}},  # tracked, id absent -> None
            {"tmdbId": 333, "hasFile": False},  # not imported -> skipped
            {"movieFile": {"id": 99, "path": "/x/no-id.mkv"}},  # no tmdbId -> skipped
        ]
        opener = _RecordingOpener(lambda req: json.dumps(movies).encode("utf-8"))
        client = _radarr(opener)

        index = client.fetch_tracked_index()

        # tmdbId -> {basename: movieFile id} (#61); a file with no usable id keeps
        # its basename with a None id so the association still forms.
        self.assertEqual(
            index,
            {"111": {"a.4k.mkv": 10}, "222": {"b.1080.mkv": 20}, "444": {"d.mkv": None}},
        )
        self.assertEqual(urlparse(opener.requests[0].full_url).path, "/api/v3/movie")

    def test_sonarr_index_maps_episode_basename_ids(self) -> None:
        series = [{"id": 1, "tvdbId": 9001}, {"id": 2, "tvdbId": 9002}]
        files_by_series = {
            "1": [{"id": 100, "path": "/tv/Show A/S01/a1.mkv"}, {"path": "/tv/Show A/S01/a2.mkv"}],
            "2": [{"id": 300, "path": "/tv/Show B/S01/b1.mkv"}, {"relativePath": "no-path.mkv"}],
        }

        def responder(req) -> bytes:
            path = urlparse(req.full_url).path
            if path == "/api/v3/series":
                return json.dumps(series).encode("utf-8")
            series_id = parse_qs(urlparse(req.full_url).query)["seriesId"][0]
            return json.dumps(files_by_series[series_id]).encode("utf-8")

        opener = _RecordingOpener(responder)
        client = _sonarr(opener)

        index = client.fetch_tracked_index()

        # basename -> [episodeFile ids-or-None] (#61); a1/b1 carry ids, a2 has a
        # path but no id (still a key, [None] — occurrence preserved so cross-series
        # ambiguity can't collapse), no-path.mkv is dropped (no path).
        self.assertEqual(index, {"a1.mkv": [100], "a2.mkv": [None], "b1.mkv": [300]})
        # one /series call plus one /episodefile call per series
        paths = [urlparse(r.full_url).path for r in opener.requests]
        self.assertEqual(paths.count("/api/v3/episodefile"), 2)

    @staticmethod
    def _sonarr_responder(series):
        """A /series + /episodefile responder: one file per series, named eN.mkv."""

        def responder(req) -> bytes:
            path = urlparse(req.full_url).path
            if path == "/api/v3/series":
                return json.dumps(series).encode("utf-8")
            sid = parse_qs(urlparse(req.full_url).query)["seriesId"][0]
            return json.dumps([{"path": f"/tv/s{sid}/e{sid}.mkv"}]).encode("utf-8")

        return responder

    def test_sonarr_aggregates_all_series_one_request_each(self) -> None:
        # The parallel fan-out (#19) still fetches each series exactly once and
        # unions every basename — no series dropped, none double-fetched.
        series = [{"id": i} for i in range(1, 21)]  # 20 series
        opener = _RecordingOpener(self._sonarr_responder(series))
        client = _sonarr(opener)

        index = client.fetch_tracked_index()

        # _sonarr_responder emits no ids, so every basename maps to [None] (one
        # occurrence, no usable id) — the key is present so matching still works.
        self.assertEqual(index, {f"e{i}.mkv": [None] for i in range(1, 21)})
        paths = [urlparse(r.full_url).path for r in opener.requests]
        self.assertEqual(paths.count("/api/v3/series"), 1)
        self.assertEqual(paths.count("/api/v3/episodefile"), 20)

    def test_sonarr_fetches_series_concurrently(self) -> None:
        # Bounded-parallel fan-out (#19): the episodefile calls must overlap, not
        # run serially. A Barrier the size of the series count releases only when
        # every worker reaches it together; serial execution would block the first
        # worker until the timeout trips the barrier, so a clean return with the
        # full basename set proves the requests ran concurrently.
        n = min(4, arr._SONARR_MAX_WORKERS)
        series = [{"id": i} for i in range(1, n + 1)]
        barrier = threading.Barrier(n, timeout=5)
        base = self._sonarr_responder(series)

        def responder(req) -> bytes:
            if urlparse(req.full_url).path == "/api/v3/episodefile":
                barrier.wait()  # every episodefile worker must be in flight at once
            return base(req)

        client = _sonarr(_RecordingOpener(responder))

        self.assertEqual(
            client.fetch_tracked_index(), {f"e{i}.mkv": [None] for i in range(1, n + 1)}
        )

    def test_sonarr_worker_failure_fails_closed(self) -> None:
        # A single failed episodefile fetch aborts the whole index rather than
        # returning a partial set — a partial index would mislabel tracked TV
        # copies as unknown and risk calling a re-downloading copy safe.
        series = [{"id": 1}, {"id": 2}, {"id": 3}]

        def responder(req) -> bytes:
            path = urlparse(req.full_url).path
            if path == "/api/v3/series":
                return json.dumps(series).encode("utf-8")
            sid = parse_qs(urlparse(req.full_url).query)["seriesId"][0]
            if sid == "2":
                raise _http_error(500, b"boom")
            return json.dumps([{"path": f"/tv/s{sid}.mkv"}]).encode("utf-8")

        client = _sonarr(_RecordingOpener(responder))

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))

    def test_sonarr_logs_progress_for_large_library(self) -> None:
        # A large library logs progress so the index doesn't look hung; the final
        # line reports completion of every series.
        count = arr._SONARR_PROGRESS_EVERY + 5
        series = [{"id": i} for i in range(1, count + 1)]
        client = _sonarr(_RecordingOpener(self._sonarr_responder(series)))

        with self.assertLogs("unraid_cache_cleaner.arr", level="INFO") as cm:
            client.fetch_tracked_index()

        progress = [line for line in cm.output if "indexed" in line]
        self.assertTrue(progress)
        self.assertTrue(any(f"{count}/{count} series" in line for line in progress))

    def test_sonarr_empty_series_makes_no_episodefile_calls(self) -> None:
        opener = _RecordingOpener(lambda req: json.dumps([]).encode("utf-8"))
        client = _sonarr(opener)

        self.assertEqual(client.fetch_tracked_index(), {})
        paths = [urlparse(r.full_url).path for r in opener.requests]
        self.assertNotIn("/api/v3/episodefile", paths)

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

    def test_read_timeout_wrapped_as_arrclienterror(self) -> None:
        # A read-phase socket.timeout is an OSError that urllib does NOT wrap in
        # URLError; it must still surface as ArrClientError so the report degrades
        # gracefully instead of crashing.
        opener = _RecordingOpener(_raiser(socket.timeout("timed out")))
        client = _radarr(opener)

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()

        self.assertIn("Unable to reach Radarr", str(ctx.exception))
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Redirect safety — real opener, fake socket layer (see tests/_fake_http.py)    #
# --------------------------------------------------------------------------- #

def _radarr_with_fake(base_url: str, responder):
    client = RadarrClient(base_url, "SECRET-KEY-123")
    fake = _FakeHTTPHandler(responder)
    client._opener.add_handler(fake)
    return client, fake


class RedirectSafetyTests(unittest.TestCase):
    def test_cross_host_redirect_refused_and_key_not_leaked(self) -> None:
        def responder(req):
            return _FakeHTTPResponse(302, "Location: http://evil.example/steal\n")

        client, fake = _radarr_with_fake("http://radarr:7878", responder)

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()

        self.assertIn("refusing to follow", str(ctx.exception))
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))
        # The redirect target is never contacted, so the API key — which rides
        # only on requests to the configured host — cannot reach it.
        hosts = [urlparse(r.full_url).hostname for r in fake.requests]
        self.assertEqual(hosts, ["radarr"])
        for req in fake.requests:
            self.assertNotIn("evil.example", req.full_url)

    def test_tls_downgrade_redirect_refused(self) -> None:
        def responder(req):
            return _FakeHTTPResponse(302, "Location: http://radarr:7878/steal\n")

        client, fake = _radarr_with_fake("https://radarr:7878", responder)

        with self.assertRaises(ArrClientError) as ctx:
            client.fetch_tracked_index()

        self.assertIn("refusing to follow", str(ctx.exception))
        self.assertEqual([urlparse(r.full_url).scheme for r in fake.requests], ["https"])

    def test_same_host_redirect_followed_and_recarries_key(self) -> None:
        movies = json.dumps(
            [{"tmdbId": 111, "movieFile": {"id": 10, "path": "/movies/A/a.mkv"}}]
        ).encode("utf-8")

        def responder(req):
            if urlparse(req.full_url).path == "/api/v3/movie":
                return _FakeHTTPResponse(302, "Location: http://radarr:7878/relocated\n")
            return _FakeHTTPResponse(200, "Content-Type: application/json\n", movies)

        client, fake = _radarr_with_fake("http://radarr:7878", responder)

        index = client.fetch_tracked_index()

        self.assertEqual(index, {"111": {"a.mkv": 10}})
        self.assertEqual([urlparse(r.full_url).path for r in fake.requests],
                         ["/api/v3/movie", "/relocated"])
        # The followed request re-carries the key (added as an unredirected
        # header on every open()).
        self.assertEqual(fake.requests[1].get_header("X-api-key"), "SECRET-KEY-123")


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


def _ids_by_name(group):
    return {c.file.name: c.arr_file_id for c in group.copies}


class AnnotateMovieTests(unittest.TestCase):
    def test_tracked_keeper_and_untracked_sibling(self) -> None:
        group = _movie_group(
            "123",
            [("/movies/M/big.4k.mkv", 20 * GiB, "4k"), ("/movies/M/big.1080.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"big.4k.mkv": 55}}, {})

        self.assertEqual(
            _by_name(out),
            {"big.4k.mkv": (arr.TRACKED, arr.RADARR), "big.1080.mkv": (arr.UNTRACKED, None)},
        )
        # the tracked copy carries its movieFile id (#61); the untracked sibling none
        self.assertEqual(_ids_by_name(out), {"big.4k.mkv": 55, "big.1080.mkv": None})
        # keeper (the 4k copy) carries the tracked association, not the default
        self.assertEqual(out.keeper.association, arr.TRACKED)
        self.assertEqual(out.keeper.arr_tracked, arr.RADARR)
        self.assertEqual(out.keeper.arr_file_id, 55)
        # classification + reclaimable math are unchanged by annotation
        self.assertEqual(out.classification, group.classification)
        self.assertEqual(out.reclaimable_bytes, group.reclaimable_bytes)

    def test_id_matches_but_no_basename_match_is_unknown(self) -> None:
        group = _movie_group(
            "123",
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"somewhere-else.mkv": 55}}, {})

        self.assertEqual(
            {c.association for c in out.copies}, {arr.UNKNOWN}
        )
        self.assertTrue(all(c.arr_tracked is None for c in out.copies))

    def test_no_id_match_is_unknown(self) -> None:
        group = _movie_group(
            "999",
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"a.mkv": 55}}, {})
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_missing_plex_id_is_unknown(self) -> None:
        group = _movie_group(
            None,
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"a.mkv": 55}}, {})
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_empty_radarr_index_leaves_movies_unknown(self) -> None:
        group = _movie_group(
            "123",
            [("/movies/M/a.mkv", 20 * GiB, "4k"), ("/movies/M/b.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {}, {})
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_ambiguous_duplicate_basename_is_unknown(self) -> None:
        # Two copies of the same movie share a filename in different dirs. Radarr
        # tracks one exact path but the basename matches both, so neither can be
        # confidently called tracked -> both unknown (never untracked/safe).
        group = _movie_group(
            "123",
            [("/movies/Real/big.mkv", 20 * GiB, "4k"), ("/movies/OldDupe/big.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"big.mkv": 55}}, {})

        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})
        self.assertTrue(all(c.arr_tracked is None for c in out.copies))
        # an ambiguous (unknown) copy is never handed a file id
        self.assertTrue(all(c.arr_file_id is None for c in out.copies))

    def test_unique_match_still_tracks_when_a_sibling_differs(self) -> None:
        # Only the ambiguous case degrades: a uniquely-named tracked copy is still
        # tracked and its differently-named sibling still untracked.
        group = _movie_group(
            "123",
            [("/movies/M/big.4k.mkv", 20 * GiB, "4k"), ("/movies/M/big.1080.mkv", 8 * GiB, "1080")],
        )
        [out] = annotate([group], {"123": {"big.4k.mkv": 55}}, {})
        self.assertEqual(
            _by_name(out),
            {"big.4k.mkv": (arr.TRACKED, arr.RADARR), "big.1080.mkv": (arr.UNTRACKED, None)},
        )

    def _stacked_group(self):
        # A stacked copy (cd1+cd2 share media_id) plus a second single copy.
        cd1 = MediaCopy(part_id=1, file=Path("/m/cd1.mkv"), size=5 * GiB, resolution="1080", media_id=10)
        cd2 = MediaCopy(part_id=2, file=Path("/m/cd2.mkv"), size=5 * GiB, resolution="1080", media_id=10)
        other = MediaCopy(part_id=3, file=Path("/m/other.mkv"), size=6 * GiB, resolution="1080", media_id=11)
        return dedupe.analyze_group(
            DuplicateGroup(
                rating_key="1",
                kind="movie",
                title="Stacked",
                copies=(cd1, cd2, other),
                external_ids={"tmdb": "123"},
            )
        )

    def test_stacked_first_part_tracked_marks_copy_tracked(self) -> None:
        [out] = annotate([self._stacked_group()], {"123": {"cd1.mkv": 51}}, {})

        by_assoc = {c.file.name: c.association for c in out.copies}
        self.assertEqual(by_assoc["cd1.mkv"], arr.TRACKED)
        self.assertEqual(by_assoc["other.mkv"], arr.UNTRACKED)
        # the id lands on the matched part only — never spread across the stack:
        # cd1 (matched) carries 51, cd2 (its stack sibling) stays None.
        ids = _ids_by_name(out)
        self.assertEqual(ids["cd1.mkv"], 51)
        self.assertIsNone(ids["cd2.mkv"])

    def test_stacked_non_first_part_tracked_still_marks_copy_tracked(self) -> None:
        # Radarr tracks cd2 (the SECOND part). The merged copy keeps cd1's fields,
        # so the whole stack must carry the tracked label or the merged copy would
        # read as safe while deleting it re-downloads cd2.
        [out] = annotate([self._stacked_group()], {"123": {"cd2.mkv": 52}}, {})

        by_assoc = {c.file.name: c.association for c in out.copies}
        self.assertEqual(by_assoc["cd1.mkv"], arr.TRACKED)
        self.assertEqual(by_assoc["other.mkv"], arr.UNTRACKED)
        # the id is pinned to cd2 (the matched part), not propagated onto cd1.
        ids = _ids_by_name(out)
        self.assertEqual(ids["cd2.mkv"], 52)
        self.assertIsNone(ids["cd1.mkv"])

    def test_mismatch_group_never_labeled_safe(self) -> None:
        # Two different films Plex merged into one group ({tmdb-111} vs {tmdb-222}).
        # Even though Radarr tracks the first film's basename, no copy may be
        # labeled untracked/safe — the grouping is not trusted.
        group = _movie_group(
            "111",
            [("/m/A {tmdb-111}/a.mkv", 5 * GiB, "1080"), ("/m/B {tmdb-222}/b.mkv", 6 * GiB, "1080")],
        )
        self.assertEqual(group.classification, dedupe.MISMATCH)

        [out] = annotate([group], {"111": {"a.mkv": 55}}, {})

        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})
        self.assertTrue(all(c.arr_tracked is None for c in out.copies))


class AnnotateEpisodeTests(unittest.TestCase):
    def test_tracked_by_basename_extra_is_unknown_not_untracked(self) -> None:
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e1.720.mkv", 2 * GiB, "720")],
        )
        [out] = annotate([group], {}, {"e1.mkv": [77]})

        self.assertEqual(
            _by_name(out),
            {"e1.mkv": (arr.TRACKED, arr.SONARR), "e1.720.mkv": (arr.UNKNOWN, None)},
        )
        # the tracked episode carries its episodeFile id; the unknown copy none
        self.assertEqual(_ids_by_name(out), {"e1.mkv": 77, "e1.720.mkv": None})

    def test_tracked_basename_shared_across_series_gets_no_id(self) -> None:
        # A basename Sonarr tracks in two series is still tracked here (it matches
        # exactly one stack in THIS group), but its id is ambiguous across series,
        # so no id is pinned — the reclaim path refuses it until it can be resolved.
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e1.720.mkv", 2 * GiB, "720")],
        )
        [out] = annotate([group], {}, {"e1.mkv": [77, 88]})

        by_name = _by_name(out)
        self.assertEqual(by_name["e1.mkv"], (arr.TRACKED, arr.SONARR))
        self.assertIsNone(_ids_by_name(out)["e1.mkv"])

    def test_shared_basename_one_missing_id_still_ambiguous(self) -> None:
        # A basename in two series where ONE file lacks an id ([77, None]) must
        # stay ambiguous — collapsing to the single id 77 would pin (and later
        # delete) the wrong series' file. It is tracked but not by-id actionable.
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e1.720.mkv", 2 * GiB, "720")],
        )
        [out] = annotate([group], {}, {"e1.mkv": [77, None]})

        self.assertEqual(_by_name(out)["e1.mkv"], (arr.TRACKED, arr.SONARR))
        self.assertIsNone(_ids_by_name(out)["e1.mkv"])

    def test_tracked_basename_no_usable_id_gets_no_id(self) -> None:
        # A tracked basename whose only file lacks a usable id ([None]) forms the
        # association but pins no id (refused by the action layer until resolvable).
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e1.720.mkv", 2 * GiB, "720")],
        )
        [out] = annotate([group], {}, {"e1.mkv": [None]})

        self.assertEqual(_by_name(out)["e1.mkv"], (arr.TRACKED, arr.SONARR))
        self.assertIsNone(_ids_by_name(out)["e1.mkv"])

    def test_no_basename_match_all_unknown(self) -> None:
        group = _episode_group(
            [("/tv/Show/S01/e1.mkv", 3 * GiB, "1080"), ("/tv/Show/S01/e2.mkv", 3 * GiB, "1080")],
        )
        [out] = annotate([group], {}, {"totally-different.mkv": [77]})
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_ambiguous_episode_basename_is_unknown(self) -> None:
        # Two episode copies share a filename; Sonarr tracks that name but can't
        # be pinned to one -> both unknown, not tracked.
        group = _episode_group(
            [("/tv/A/e1.mkv", 3 * GiB, "1080"), ("/tv/B/e1.mkv", 3 * GiB, "1080")],
        )
        [out] = annotate([group], {}, {"e1.mkv": [77]})
        self.assertEqual({c.association for c in out.copies}, {arr.UNKNOWN})

    def test_other_kind_passes_through_untouched(self) -> None:
        copies = (
            MediaCopy(part_id=1, file=Path("/x/a"), size=GiB, media_id=1),
            MediaCopy(part_id=2, file=Path("/x/b"), size=GiB, media_id=2),
        )
        group = dedupe.analyze_group(
            DuplicateGroup(rating_key="1", kind="other", title="X", copies=copies)
        )
        [out] = annotate([group], {"1": {"a": 1}}, {"a": [1]})
        self.assertIs(out, group)


# --------------------------------------------------------------------------- #
# Mutation / resolve methods (#34 Phase 2)                                     #
# --------------------------------------------------------------------------- #

class AsIntTests(unittest.TestCase):
    def test_positive_ints_pass_non_positive_and_malformed_become_none(self) -> None:
        # An *arr file id is always a positive integer, so 0 (a common "unset"
        # sentinel), negatives, and non-numeric values all read as absent — never
        # as an addressable delete target (#61).
        self.assertEqual(arr._as_int(42), 42)
        self.assertEqual(arr._as_int("7"), 7)
        self.assertIsNone(arr._as_int(0))
        self.assertIsNone(arr._as_int(-1))
        self.assertIsNone(arr._as_int("not-a-number"))
        self.assertIsNone(arr._as_int(None))


class ArrMutationTests(unittest.TestCase):
    def test_radarr_get_movie_file_reads_by_id(self) -> None:
        # The by-id re-validation the web action layer runs before a delete (#61):
        # one GET /moviefile/{id} returning the current file record.
        opener = _RecordingOpener(
            lambda req: json.dumps({"id": 42, "path": "/movies/A/old.mkv"}).encode("utf-8")
        )
        client = _radarr(opener)
        record = client.get_movie_file(42)
        self.assertEqual(record["path"], "/movies/A/old.mkv")
        self.assertEqual(urlparse(opener.requests[-1].full_url).path, "/api/v3/moviefile/42")
        self.assertEqual(opener.requests[-1].get_method(), "GET")

    def test_radarr_get_movie_file_404_carries_status(self) -> None:
        # A removed/reused id surfaces as ArrClientError(status_code=404) so the
        # caller can refuse precisely rather than deleting the wrong file.
        client = _radarr(_RecordingOpener(_raiser(_http_error(404, b"gone"))))
        with self.assertRaises(ArrClientError) as ctx:
            client.get_movie_file(7)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_radarr_get_movie_file_non_object_fails_closed(self) -> None:
        # A non-object body (a top-level list) is refused, not silently accepted.
        client = _radarr(_RecordingOpener(lambda req: b"[]"))
        with self.assertRaises(ArrClientError):
            client.get_movie_file(1)

    def test_sonarr_get_episode_file_reads_by_id(self) -> None:
        opener = _RecordingOpener(
            lambda req: json.dumps({"id": 99, "path": "/tv/A/S01/ep.mkv"}).encode("utf-8")
        )
        client = _sonarr(opener)
        record = client.get_episode_file(99)
        self.assertEqual(record["path"], "/tv/A/S01/ep.mkv")
        self.assertEqual(urlparse(opener.requests[-1].full_url).path, "/api/v3/episodefile/99")

    def test_radarr_delete_issues_delete_verb_at_moviefile_path(self) -> None:
        opener = _RecordingOpener(lambda req: b"{}")
        client = _radarr(opener)
        client.delete_movie_file(42)
        request = opener.requests[-1]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(urlparse(request.full_url).path, "/api/v3/moviefile/42")

    def test_radarr_delete_maps_http_error_to_arrclienterror(self) -> None:
        client = _radarr(_RecordingOpener(_raiser(_http_error(404, b"gone"))))
        with self.assertRaises(ArrClientError) as ctx:
            client.delete_movie_file(7)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_sonarr_delete_issues_delete_verb_at_episodefile_path(self) -> None:
        opener = _RecordingOpener(lambda req: b"{}")
        client = _sonarr(opener)
        client.delete_episode_file(99)
        request = opener.requests[-1]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(urlparse(request.full_url).path, "/api/v3/episodefile/99")

    def test_delete_is_not_retried(self) -> None:
        # A DELETE that times out must NOT be replayed (ambiguous outcome), even
        # with retries configured for idempotent reads.
        calls = {"n": 0}

        def responder(_req):
            calls["n"] += 1
            raise urllib.error.URLError("timed out")

        client = RadarrClient("http://radarr:7878", "K", max_attempts=3)
        client._opener = _RecordingOpener(responder)
        with self.assertRaises(ArrClientError):
            client.delete_movie_file(1)
        self.assertEqual(calls["n"], 1)  # single attempt, no retry


if __name__ == "__main__":
    unittest.main()
