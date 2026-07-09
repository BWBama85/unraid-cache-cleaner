"""Minimal qBittorrent WebUI API client."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .http_client import JsonHttpClient
from .models import TorrentRecord


class QbittorrentClientError(RuntimeError):
    """Raised when qBittorrent cannot be queried safely."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QbittorrentClient(JsonHttpClient):
    """Small authenticated client for the qBittorrent WebUI API.

    Shares the fail-closed opener + transport taxonomy of
    :class:`~unraid_cache_cleaner.http_client.JsonHttpClient`, but keeps its own
    request flow: a session ``CookieJar``, a ``Referer`` header, form-encoded
    login POST, plaintext responses, and a one-shot 403 re-authentication.
    """

    service_name = "qBittorrent"
    error_class = QbittorrentClientError

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: int = 15,
        verify_tls: bool = True,
    ) -> None:
        self.username = username
        self.password = password
        self._cookie_jar = CookieJar()
        self._authenticated = False
        super().__init__(base_url, timeout_seconds=timeout_seconds, verify_tls=verify_tls)

    def _extra_handlers(self) -> Sequence[urllib.request.BaseHandler]:
        return (urllib.request.HTTPCookieProcessor(self._cookie_jar),)

    def _auth_headers(self) -> Sequence[Tuple[str, str]]:
        return (("Referer", self.base_url),)

    def _request(
        self,
        method: str,
        api_path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        form_data: Optional[Dict[str, str]] = None,
        allow_reauth: bool = True,
    ) -> str:
        request_data = None
        headers = {}
        if form_data is not None:
            request_data = urllib.parse.urlencode(form_data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(
            self._build_url(api_path, params),
            data=request_data,
            method=method,
            headers=headers,
        )

        try:
            return self._read_text(request)
        except QbittorrentClientError as exc:
            # ``status_code`` is set by the base ``_on_http_error``; only an HTTP
            # response carries one, so a connect/read failure (status_code None)
            # correctly falls through to re-raise.
            if exc.status_code == 403 and allow_reauth and api_path != "/api/v2/auth/login":
                self.login(force=True)
                return self._request(
                    method,
                    api_path,
                    params=params,
                    form_data=form_data,
                    allow_reauth=False,
                )
            raise

    def login(self, *, force: bool = False) -> None:
        """Authenticate against the WebUI API."""

        if self._authenticated and not force:
            return
        if not self.username:
            raise QbittorrentClientError(
                "QBITTORRENT_USERNAME is required. Do not rely on container-local localhost access."
            )

        response = self._request(
            "POST",
            "/api/v2/auth/login",
            form_data={
                "username": self.username,
                "password": self.password,
            },
            allow_reauth=False,
        ).strip()
        # "Ok." is the normal success response. When this client is exempt from
        # authentication (qBittorrent's "bypass authentication for localhost" or a
        # whitelisted subnet), the login endpoint instead returns an empty 204
        # body. Treat that as success too; only a non-empty, non-"Ok." body such
        # as "Fails." is an actual login failure.
        if response not in ("Ok.", ""):
            raise QbittorrentClientError(f"qBittorrent login failed: {response}")
        self._authenticated = True

    def fetch_default_save_path(self) -> Path:
        """Return the default save path configured in qBittorrent."""

        self.login()
        response = self._request("GET", "/api/v2/app/defaultSavePath")
        return Path(response.strip())

    def fetch_torrents(self) -> list[TorrentRecord]:
        """Return the current torrent list."""

        self.login()
        response = self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"filter": "all"},
        )
        payload = json.loads(response)
        torrents: list[TorrentRecord] = []
        for item in payload:
            save_path = Path(item.get("save_path") or "")
            content_path = Path(item.get("content_path") or save_path / item.get("name", ""))
            torrents.append(
                TorrentRecord(
                    torrent_hash=item.get("hash", ""),
                    name=item.get("name", ""),
                    state=item.get("state", ""),
                    save_path=save_path,
                    content_path=content_path,
                    progress=float(item.get("progress", 0.0)),
                )
            )
        return torrents
