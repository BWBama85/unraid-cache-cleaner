"""Shared fake ``urllib`` socket layer for the client redirect tests.

The redirect-safety tests need a *real* opener so the redirect handler,
``addheaders``, and cookie processor all run as in production, but with the
socket layer replaced. :class:`FakeHTTPHandler` sorts ahead of the default
HTTP(S) handlers and answers with a canned :class:`FakeHTTPResponse` before a
real socket is opened. Kept in one place so the intercept contract (the
``http.client.HTTPResponse`` surface the opener reads) is defined once for
``test_plex``, ``test_arr``, and ``test_qbittorrent``.
"""

from __future__ import annotations

import email
import io
import urllib.request


class FakeHTTPResponse:
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

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeHTTPHandler(urllib.request.BaseHandler):
    """Intercepts the socket layer of a *real* opener; records every request.

    Sorts ahead of the default HTTP(S) handlers (``handler_order``) so it answers
    before a real socket is opened, while the opener's cookie processor, redirect
    handler, and ``addheaders`` (the credential) all run exactly as in production.
    """

    handler_order = 100

    def __init__(self, responder) -> None:
        self._responder = responder
        self.requests = []

    def http_open(self, req):
        self.requests.append(req)
        return self._responder(req)

    https_open = http_open
