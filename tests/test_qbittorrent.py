"""Tests for the qBittorrent client login and redirect handling."""

from __future__ import annotations

import email
import io
import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.qbittorrent import QbittorrentClient, QbittorrentClientError


# --------------------------------------------------------------------------- #
# Redirect safety — real opener, fake socket layer (mirrors tests/test_plex.py) #
# --------------------------------------------------------------------------- #

class _FakeHTTPResponse:
    """Minimal stand-in for ``http.client.HTTPResponse`` for the real opener."""

    def __init__(self, code: int, header_text: str, body: bytes = b"") -> None:
        self.code = code
        self.status = code
        self.msg = "Testing"
        self._info = email.message_from_string(header_text)
        self._buf = io.BytesIO(body)

    def info(self):
        return self._info

    def geturl(self) -> str:
        return ""

    def read(self, amt: int | None = None) -> bytes:
        return self._buf.read() if amt is None else self._buf.read(amt)

    def close(self) -> None:
        pass

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeHTTPHandler(urllib.request.BaseHandler):
    """Intercepts the socket layer of a *real* opener; records every request.

    Sorts ahead of the default HTTP(S) handlers (``handler_order``) so it answers
    before a real socket is opened, while the opener's cookie processor, redirect
    handler, and ``addheaders`` (the ``Referer``) all run as in production.
    """

    handler_order = 100

    def __init__(self, responder) -> None:
        self._responder = responder
        self.requests = []

    def http_open(self, req):
        self.requests.append(req)
        return self._responder(req)

    https_open = http_open


def _client_with_fake(base_url: str, responder):
    client = QbittorrentClient(base_url, "admin", "secret")
    fake = _FakeHTTPHandler(responder)
    client._opener.add_handler(fake)
    return client, fake


_LOGIN_HEADERS = "Set-Cookie: SID=SECRET-SID-COOKIE; path=/\n"


class RedirectSafetyTests(unittest.TestCase):
    def test_cross_host_redirect_refused_no_cookie_or_referer_leak(self) -> None:
        def responder(req):
            if req.get_method() == "POST":  # /api/v2/auth/login
                return _FakeHTTPResponse(200, _LOGIN_HEADERS, b"Ok.")
            return _FakeHTTPResponse(302, "Location: http://evil.example/steal\n")

        client, fake = _client_with_fake("http://qbt:8080", responder)

        with self.assertRaises(QbittorrentClientError) as ctx:
            client.fetch_torrents()

        self.assertIn("refusing to follow", str(ctx.exception))
        # Login populated the SID cookie, so a credential existed that *could*
        # have leaked — the guard is what stops it.
        self.assertIn("SID", {c.name for c in client._cookie_jar})
        # The redirect target is never contacted, so neither the SID cookie nor
        # the Referer base URL can reach it.
        hosts = [urlparse(r.full_url).hostname for r in fake.requests]
        self.assertEqual(set(hosts), {"qbt"})
        for req in fake.requests:
            self.assertNotIn("evil.example", req.full_url)

    def test_tls_downgrade_redirect_refused(self) -> None:
        def responder(req):
            if req.get_method() == "POST":
                return _FakeHTTPResponse(200, _LOGIN_HEADERS, b"Ok.")
            return _FakeHTTPResponse(302, "Location: http://qbt:8080/steal\n")

        client, fake = _client_with_fake("https://qbt:8080", responder)

        with self.assertRaises(QbittorrentClientError) as ctx:
            client.fetch_torrents()

        self.assertIn("refusing to follow", str(ctx.exception))
        self.assertEqual({urlparse(r.full_url).scheme for r in fake.requests}, {"https"})

    def test_same_host_redirect_followed_recarries_cookie_and_referer(self) -> None:
        torrents = json.dumps(
            [{"hash": "abc", "name": "t", "state": "pausedUP",
              "save_path": "/data", "content_path": "/data/t", "progress": 1.0}]
        ).encode("utf-8")

        def responder(req):
            if req.get_method() == "POST":
                return _FakeHTTPResponse(200, _LOGIN_HEADERS, b"Ok.")
            if urlparse(req.full_url).path == "/api/v2/torrents/info":
                return _FakeHTTPResponse(302, "Location: http://qbt:8080/relocated\n")
            return _FakeHTTPResponse(200, "Content-Type: application/json\n", torrents)

        client, fake = _client_with_fake("http://qbt:8080", responder)

        result = client.fetch_torrents()

        self.assertEqual(len(result), 1)
        followed = fake.requests[-1]
        self.assertEqual(urlparse(followed.full_url).path, "/relocated")
        # The followed same-host request still carries the cookie and Referer.
        self.assertIn("SID=SECRET-SID-COOKIE", followed.get_header("Cookie", ""))
        self.assertEqual(followed.get_header("Referer"), "http://qbt:8080")


class LoginTests(unittest.TestCase):
    def _client(self) -> QbittorrentClient:
        return QbittorrentClient("http://qbt:8080", "admin", "secret")

    def test_login_accepts_ok(self) -> None:
        client = self._client()
        with mock.patch.object(client, "_request", return_value="Ok."):
            client.login()
        self.assertTrue(client._authenticated)

    def test_login_accepts_empty_bypass_response(self) -> None:
        # qBittorrent returns an empty 204 body when the client is auth-bypassed
        # (whitelisted subnet or localhost). That must count as success.
        client = self._client()
        with mock.patch.object(client, "_request", return_value=""):
            client.login()
        self.assertTrue(client._authenticated)

    def test_login_rejects_fails(self) -> None:
        client = self._client()
        with mock.patch.object(client, "_request", return_value="Fails."):
            with self.assertRaises(QbittorrentClientError):
                client.login()
        self.assertFalse(client._authenticated)


if __name__ == "__main__":
    unittest.main()
