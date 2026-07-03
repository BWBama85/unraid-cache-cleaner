"""Shared fail-closed redirect handler for the ``urllib`` service clients.

Every client (:mod:`plex`, :mod:`arr`, :mod:`qbittorrent`) authenticates by
attaching a secret to the opener's ``addheaders`` (or a domain-scoped cookie).
urllib re-applies those headers as *unredirected* headers on every request it
opens — including the one it builds internally to follow a 3xx — so a
misconfigured endpoint or an interposing reverse proxy that 301/302s to a
different host would otherwise receive the secret. :class:`HostBoundRedirectHandler`
is the one guard all three share (see #12 for the original Plex-only fix and #22
for the extraction); when the shared JSON-HTTP base of #20 lands it becomes the
base's redirect handler.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Callable, Optional


class HostBoundRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that would carry credentials off the configured host.

    A redirect is followed only when it stays on ``allowed_host`` and does not
    downgrade TLS (an ``https`` deployment never follows a redirect to plaintext
    ``http``, which would leak the secret in the clear). Everything else raises —
    via ``error_factory``, so each client surfaces its own ``*ClientError`` —
    before the redirected request is issued. Same-host port changes and
    ``http`` -> ``https`` upgrades — common reverse-proxy behaviour — stay allowed.

    ``error_factory`` is called ``error_factory(message, status_code=code)``; the
    per-client ``*ClientError`` types all accept that signature. ``service_name``
    only labels the message (e.g. ``"Plex"``) — never interpolate a secret into it.
    """

    def __init__(
        self,
        allowed_host: str,
        *,
        require_tls: bool,
        service_name: str,
        error_factory: Callable[..., Exception],
    ) -> None:
        super().__init__()
        self._allowed_host = allowed_host.lower()
        self._require_tls = require_tls
        self._service_name = service_name
        self._error_factory = error_factory

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        target = urllib.parse.urlparse(newurl)
        target_host = (target.hostname or "").lower()
        if target_host != self._allowed_host or (self._require_tls and target.scheme != "https"):
            raise self._error_factory(
                f"{self._service_name} redirected to "
                f"'{target.scheme}://{target.hostname or 'unknown'}'; refusing to follow a "
                "cross-host or TLS-downgrading redirect so credentials stay on-box.",
                status_code=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_handler(
    base_url: str,
    *,
    service_name: str,
    error_factory: Callable[..., Exception],
) -> HostBoundRedirectHandler:
    """Construct a :class:`HostBoundRedirectHandler` bound to ``base_url``.

    Derives the allowed host and whether to require TLS from ``base_url`` — an
    ``https`` base refuses redirects that downgrade to ``http`` — so callers don't
    repeat the ``urlparse`` dance in every ``_build_opener``.
    """

    parsed = urllib.parse.urlparse(base_url)
    return HostBoundRedirectHandler(
        parsed.hostname or "",
        require_tls=parsed.scheme == "https",
        service_name=service_name,
        error_factory=error_factory,
    )
