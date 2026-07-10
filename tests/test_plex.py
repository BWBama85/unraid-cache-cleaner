"""Tests for the Plex Web API client (no network — fake transport)."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fake_http import FakeHTTPHandler as _FakeHTTPHandler
from _fake_http import FakeHTTPResponse as _FakeHTTPResponse
from unraid_cache_cleaner.models import PlexSection
from unraid_cache_cleaner.plex import PlexClient, PlexClientError


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
    """Stand-in for the urllib opener: records requests, returns canned bodies."""

    def __init__(self, responder) -> None:
        self.requests = []
        self._responder = responder

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _FakeResponse(self._responder(request))


def _client(opener) -> PlexClient:
    client = PlexClient("http://plex:32400", "SECRET-TOKEN-123")
    client._opener = opener
    return client


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://plex:32400", code, "err", {}, io.BytesIO(body))


def _raiser(exc: Exception):
    """Return a responder that raises ``exc`` when the opener is called."""

    def responder(_request):
        raise exc

    return responder


def _client_with_fake(base_url: str, responder):
    client = PlexClient(base_url, "SECRET-TOKEN-123")
    fake = _FakeHTTPHandler(responder)
    client._opener.add_handler(fake)
    return client, fake


class RedirectSafetyTests(unittest.TestCase):
    def test_cross_host_redirect_refused_and_token_not_leaked(self) -> None:
        def responder(req):
            return _FakeHTTPResponse(302, "Location: http://evil.example/steal\n")

        client, fake = _client_with_fake("http://plex:32400", responder)

        with self.assertRaises(PlexClientError) as ctx:
            client.fetch_sections()

        self.assertIn("refusing to follow", str(ctx.exception))
        self.assertNotIn("SECRET-TOKEN-123", str(ctx.exception))
        # The redirect target is never contacted, so the token — which rides only
        # on requests to the configured host — cannot reach it.
        hosts = [urlparse(r.full_url).hostname for r in fake.requests]
        self.assertEqual(hosts, ["plex"])
        for req in fake.requests:
            self.assertNotIn("evil.example", req.full_url)

    def test_same_host_redirect_followed_and_stays_authenticated(self) -> None:
        sections = json.dumps(
            {"MediaContainer": {"Directory": [{"key": "1", "type": "movie", "title": "Movies"}]}}
        ).encode("utf-8")

        def responder(req):
            if urlparse(req.full_url).path == "/library/sections":
                return _FakeHTTPResponse(302, "Location: http://plex:32400/relocated\n")
            return _FakeHTTPResponse(200, "Content-Type: application/json\n", sections)

        client, fake = _client_with_fake("http://plex:32400", responder)

        result = client.fetch_sections()

        self.assertEqual(result, [PlexSection(key="1", type="movie", title="Movies")])
        # Two hops, both on the configured host; the followed request re-carries
        # the token (added as an unredirected header on every open()).
        self.assertEqual([urlparse(r.full_url).path for r in fake.requests],
                         ["/library/sections", "/relocated"])
        self.assertEqual(fake.requests[1].get_header("X-plex-token"), "SECRET-TOKEN-123")


class PlexClientTests(unittest.TestCase):
    def test_requires_url_and_token(self) -> None:
        with self.assertRaises(PlexClientError):
            PlexClient("", "tok")
        with self.assertRaises(PlexClientError):
            PlexClient("http://plex:32400", "")

    def test_token_and_accept_are_headers_not_url(self) -> None:
        client = PlexClient("http://plex:32400", "SECRET-TOKEN-123")
        self.assertIn(("X-Plex-Token", "SECRET-TOKEN-123"), client._opener.addheaders)
        self.assertIn(("Accept", "application/json"), client._opener.addheaders)

    def test_fetch_sections_parses_directory(self) -> None:
        body = json.dumps(
            {
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "Movies"},
                        {"key": "2", "type": "show", "title": "TV Shows"},
                    ]
                }
            }
        ).encode("utf-8")
        opener = _RecordingOpener(lambda req: body)
        client = _client(opener)

        sections = client.fetch_sections()

        self.assertEqual(
            sections,
            [
                PlexSection(key="1", type="movie", title="Movies"),
                PlexSection(key="2", type="show", title="TV Shows"),
            ],
        )
        self.assertEqual(urlparse(opener.requests[0].full_url).path, "/library/sections")

    def test_fetch_duplicates_paginates_to_total_size(self) -> None:
        total = 250
        page_size = 200

        def responder(req) -> bytes:
            start = int(req.get_header("X-plex-container-start"))
            page = [
                {"ratingKey": str(i)}
                for i in range(start, min(start + page_size, total))
            ]
            return json.dumps(
                {"MediaContainer": {"totalSize": total, "size": len(page), "Metadata": page}}
            ).encode("utf-8")

        opener = _RecordingOpener(responder)
        client = _client(opener)

        items = client.fetch_duplicates("1", 1, page_size=page_size)

        self.assertEqual(len(items), total)
        # exactly two pages fetched, advancing the container-start header
        starts = [r.get_header("X-plex-container-start") for r in opener.requests]
        sizes = [r.get_header("X-plex-container-size") for r in opener.requests]
        self.assertEqual(starts, ["0", "200"])
        self.assertEqual(sizes, ["200", "200"])
        # every request carried the duplicate/type query flags
        for req in opener.requests:
            q = parse_qs(urlparse(req.full_url).query)
            self.assertEqual(q["type"], ["1"])
            self.assertEqual(q["duplicate"], ["1"])
            self.assertEqual(q["includeGuids"], ["1"])
            self.assertEqual(urlparse(req.full_url).path, "/library/sections/1/all")

    def test_fetch_duplicates_handles_short_pages(self) -> None:
        # Server caps each page at 100 even though we request 200; the client
        # must advance by the returned count, not the requested size, or it
        # would skip the items in between.
        total = 250
        server_cap = 100

        def responder(req) -> bytes:
            start = int(req.get_header("X-plex-container-start"))
            page = [
                {"ratingKey": str(i)}
                for i in range(start, min(start + server_cap, total))
            ]
            return json.dumps(
                {"MediaContainer": {"totalSize": total, "size": len(page), "Metadata": page}}
            ).encode("utf-8")

        client = _client(_RecordingOpener(responder))

        items = client.fetch_duplicates("1", 1, page_size=200)

        keys = [item["ratingKey"] for item in items]
        self.assertEqual(keys, [str(i) for i in range(total)])

    def test_fetch_duplicates_tolerates_null_total_size(self) -> None:
        # A server that omits/nulls totalSize must not crash: iterate until an
        # empty page instead of int(None).
        pages = [
            {"MediaContainer": {"totalSize": None, "Metadata": [{"ratingKey": "1"}]}},
            {"MediaContainer": {"totalSize": None, "Metadata": []}},
        ]

        def responder(req) -> bytes:
            return json.dumps(pages.pop(0)).encode("utf-8")

        client = _client(_RecordingOpener(responder))

        items = client.fetch_duplicates("1", 1)

        self.assertEqual(items, [{"ratingKey": "1"}])

    def test_fetch_duplicates_single_page(self) -> None:
        page = [{"ratingKey": "1"}, {"ratingKey": "2"}]
        body = json.dumps(
            {"MediaContainer": {"totalSize": 2, "size": 2, "Metadata": page}}
        ).encode("utf-8")
        opener = _RecordingOpener(lambda req: body)
        client = _client(opener)

        items = client.fetch_duplicates("1", 1)

        self.assertEqual(len(items), 2)
        self.assertEqual(len(opener.requests), 1)

    def test_401_raises_recopy_message(self) -> None:
        opener = _RecordingOpener(_raiser(_http_error(401, b"denied")))
        client = _client(opener)

        with self.assertRaises(PlexClientError) as ctx:
            client.fetch_sections()

        self.assertIn("Re-copy X-Plex-Token", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_404_section_skipped_returns_empty(self) -> None:
        opener = _RecordingOpener(_raiser(_http_error(404, b"no such section")))
        client = _client(opener)

        with self.assertLogs("unraid_cache_cleaner.plex", level="WARNING"):
            result = client.fetch_duplicates("99", 1)

        self.assertEqual(result, [])

    def test_connection_failure_message_and_no_token_leak(self) -> None:
        opener = _RecordingOpener(_raiser(urllib.error.URLError("connection refused")))
        client = _client(opener)

        with self.assertRaises(PlexClientError) as ctx:
            client.fetch_sections()

        message = str(ctx.exception)
        self.assertIn("Unable to connect to Plex at http://plex:32400", message)
        self.assertNotIn("SECRET-TOKEN-123", message)
        # token travels as a header, never in the request URL
        self.assertNotIn("SECRET-TOKEN-123", opener.requests[0].full_url)

    def test_token_never_in_http_error_message(self) -> None:
        opener = _RecordingOpener(_raiser(_http_error(500, b"boom")))
        client = _client(opener)

        with self.assertRaises(PlexClientError) as ctx:
            client.fetch_sections()

        self.assertNotIn("SECRET-TOKEN-123", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 500)


class NonObjectBodyTests(unittest.TestCase):
    def test_top_level_array_becomes_plex_error_naming_the_path(self) -> None:
        # A Plex endpoint that returned a top-level JSON array (not the usual
        # {"MediaContainer": ...} object) must surface as PlexClientError naming
        # the endpoint — not a bare AttributeError on .get("MediaContainer") that
        # fetch_duplicates' 404 handler would let escape.
        opener = _RecordingOpener(lambda req: json.dumps([{"x": 1}]).encode("utf-8"))
        client = _client(opener)

        with self.assertRaises(PlexClientError) as ctx:
            client.fetch_sections()

        self.assertIn("non-object JSON body from /library/sections", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
