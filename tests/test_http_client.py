"""Tests for the shared ``JsonHttpClient`` base (no network — fake transport).

The three service clients (:mod:`plex`, :mod:`arr`, :mod:`qbittorrent`) exercise
this base through their own suites; these tests pin the base contract directly so
a future edit to the shared plumbing is caught at the source.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Optional, Sequence, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fake_http import FakeHTTPHandler as _FakeHTTPHandler
from _fake_http import FakeHTTPResponse as _FakeHTTPResponse
from unraid_cache_cleaner.http_client import JsonHttpClient


class _DemoError(RuntimeError):
    """Stand-in ``*ClientError`` mirroring the real clients' signature."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _DemoClient(JsonHttpClient):
    """Minimal concrete subclass: one secret auth header."""

    service_name = "Demo"
    error_class = _DemoError

    def __init__(self, base_url: str, token: str, **kwargs) -> None:
        self.token = token
        kwargs.setdefault("timeout_seconds", 5)
        kwargs.setdefault("verify_tls", True)
        super().__init__(base_url, **kwargs)

    def _auth_headers(self) -> Sequence[Tuple[str, str]]:
        return (("X-Demo-Token", self.token),)

    # Expose the protected helpers for the transport tests.
    def get_json(self, api_path, params=None):
        return self._get_json(api_path, params)


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
    def __init__(self, responder) -> None:
        self.requests = []
        self._responder = responder

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _FakeResponse(self._responder(request))


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://demo:9000", code, "err", {}, io.BytesIO(body))


def _raiser(exc: Exception):
    def responder(_request):
        raise exc

    return responder


def _client(opener) -> _DemoClient:
    client = _DemoClient("http://demo:9000", "SECRET-TOKEN")
    client._opener = opener
    return client


class OpenerConstructionTests(unittest.TestCase):
    def test_base_url_is_stripped_of_trailing_slash(self) -> None:
        self.assertEqual(_DemoClient("http://demo:9000/", "t").base_url, "http://demo:9000")

    def test_auth_header_and_user_agent_are_headers_not_url(self) -> None:
        client = _DemoClient("http://demo:9000", "SECRET-TOKEN")
        keys = [k for k, _ in client._opener.addheaders]
        self.assertIn(("X-Demo-Token", "SECRET-TOKEN"), client._opener.addheaders)
        # The User-Agent is always appended after the subclass's auth headers.
        self.assertEqual(keys[-1], "User-Agent")

    def test_build_url_appends_query_only_when_params_present(self) -> None:
        client = _DemoClient("http://demo:9000", "t")
        self.assertEqual(client._build_url("/x"), "http://demo:9000/x")
        self.assertEqual(client._build_url("/x", {"a": "1"}), "http://demo:9000/x?a=1")


class TaxonomyTests(unittest.TestCase):
    def test_get_json_returns_parsed_body(self) -> None:
        opener = _RecordingOpener(lambda req: json.dumps({"ok": True}).encode("utf-8"))
        client = _client(opener)
        self.assertEqual(client.get_json("/thing"), {"ok": True})
        self.assertEqual(urlparse(opener.requests[0].full_url).path, "/thing")

    def test_http_error_carries_status_and_service_name(self) -> None:
        client = _client(_RecordingOpener(_raiser(_http_error(500, b"boom"))))
        with self.assertRaises(_DemoError) as ctx:
            client.get_json("/thing")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("HTTP 500 from Demo", str(ctx.exception))

    def test_url_error_maps_to_error_class_with_base_url(self) -> None:
        client = _client(_RecordingOpener(_raiser(urllib.error.URLError("refused"))))
        with self.assertRaises(_DemoError) as ctx:
            client.get_json("/thing")
        self.assertIn("Unable to connect to Demo at http://demo:9000", str(ctx.exception))

    def test_read_phase_os_error_is_wrapped(self) -> None:
        # socket.timeout is an OSError urllib does NOT wrap in URLError; the base
        # must still normalize it onto error_class so a read degrades gracefully.
        client = _client(_RecordingOpener(_raiser(socket.timeout("timed out"))))
        with self.assertRaises(_DemoError) as ctx:
            client.get_json("/thing")
        self.assertIn("Unable to reach Demo", str(ctx.exception))

    def test_invalid_json_becomes_error_class(self) -> None:
        client = _client(_RecordingOpener(lambda req: b"not json"))
        with self.assertRaises(_DemoError) as ctx:
            client.get_json("/thing")
        self.assertIn("Demo returned invalid JSON from /thing", str(ctx.exception))


class SecretSafetyTests(unittest.TestCase):
    def test_secret_never_in_url_or_transport_error(self) -> None:
        opener = _RecordingOpener(_raiser(urllib.error.URLError("refused")))
        client = _client(opener)
        with self.assertRaises(_DemoError) as ctx:
            client.get_json("/thing", {"q": "x"})
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))
        self.assertNotIn("SECRET-TOKEN", opener.requests[0].full_url)

    def test_cross_host_redirect_refused_secret_not_leaked(self) -> None:
        # Real opener, fake socket layer: the shared redirect guard must refuse a
        # cross-host redirect before the secret header reaches the target.
        def responder(req):
            return _FakeHTTPResponse(302, "Location: http://evil.example/steal\n")

        client = _DemoClient("http://demo:9000", "SECRET-TOKEN")
        fake = _FakeHTTPHandler(responder)
        client._opener.add_handler(fake)

        with self.assertRaises(_DemoError) as ctx:
            client._get_json("/thing")

        self.assertIn("refusing to follow", str(ctx.exception))
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))
        self.assertEqual([urlparse(r.full_url).hostname for r in fake.requests], ["demo"])
        for req in fake.requests:
            self.assertNotIn("evil.example", req.full_url)


if __name__ == "__main__":
    unittest.main()
