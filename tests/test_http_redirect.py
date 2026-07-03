"""Tests for the shared fail-closed redirect handler (no network)."""

from __future__ import annotations

import sys
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.http_redirect import HostBoundRedirectHandler, build_handler


class _Boom(RuntimeError):
    """Stand-in for a client's ``*ClientError`` (mirrors their signature)."""

    def __init__(self, message: str, *, status_code=None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _handler(base_url: str) -> HostBoundRedirectHandler:
    return build_handler(base_url, service_name="Svc", error_factory=_Boom)


class HostBoundRedirectHandlerTests(unittest.TestCase):
    def test_rejects_cross_host(self) -> None:
        handler = _handler("http://svc:8080")
        req = urllib.request.Request("http://svc:8080/thing")
        with self.assertRaises(_Boom) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/steal")
        self.assertIn("refusing to follow", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 302)

    def test_rejects_tls_downgrade(self) -> None:
        handler = _handler("https://svc:8080")
        req = urllib.request.Request("https://svc:8080/thing")
        with self.assertRaises(_Boom):
            handler.redirect_request(req, None, 302, "Found", {}, "http://svc:8080/thing")

    def test_allows_same_host(self) -> None:
        handler = _handler("http://svc:8080")
        req = urllib.request.Request("http://svc:8080/thing")
        new = handler.redirect_request(req, None, 302, "Found", {}, "http://svc:8080/relocated")
        self.assertIsInstance(new, urllib.request.Request)
        self.assertEqual(urlparse(new.full_url).path, "/relocated")

    def test_allows_same_host_port_change(self) -> None:
        # Reverse proxies routinely move the backend to another port on the same
        # host; that must stay allowed.
        handler = _handler("http://svc:8080")
        req = urllib.request.Request("http://svc:8080/thing")
        new = handler.redirect_request(req, None, 302, "Found", {}, "http://svc:9090/thing")
        self.assertIsInstance(new, urllib.request.Request)

    def test_allows_tls_upgrade_same_host(self) -> None:
        # http -> https on the configured host is an upgrade, never a leak.
        handler = _handler("http://svc:8080")
        req = urllib.request.Request("http://svc:8080/thing")
        new = handler.redirect_request(req, None, 302, "Found", {}, "https://svc/thing")
        self.assertIsInstance(new, urllib.request.Request)

    def test_error_factory_type_is_propagated(self) -> None:
        # The handler must raise the caller's own error type so each client keeps
        # catching its own ``*ClientError``.
        handler = _handler("http://svc:8080")
        req = urllib.request.Request("http://svc:8080/thing")
        with self.assertRaises(_Boom):
            handler.redirect_request(req, None, 301, "Moved", {}, "http://other/thing")

    def test_message_carries_service_name_and_target(self) -> None:
        handler = _handler("http://svc:8080")
        req = urllib.request.Request("http://svc:8080/thing")
        with self.assertRaises(_Boom) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/steal")
        message = str(ctx.exception)
        self.assertIn("Svc", message)
        self.assertIn("evil.example", message)


if __name__ == "__main__":
    unittest.main()
