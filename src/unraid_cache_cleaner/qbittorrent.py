"""Minimal qBittorrent WebUI API client."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, Optional

from .models import TorrentRecord


class QbittorrentClientError(RuntimeError):
    """Raised when qBittorrent cannot be queried safely."""


class QbittorrentClient:
    """Small authenticated client for the qBittorrent WebUI API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: int = 15,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self._cookie_jar = CookieJar()
        self._opener = self._build_opener()
        self._authenticated = False

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
        ]

        if self.base_url.startswith("https://"):
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))

        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = [
            ("Referer", self.base_url),
            ("User-Agent", "unraid-cache-cleaner/0.1.0"),
        ]
        return opener

    def _request(
        self,
        method: str,
        api_path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        form_data: Optional[Dict[str, str]] = None,
        allow_reauth: bool = True,
    ) -> str:
        encoded_params = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{api_path}"
        if encoded_params:
            url = f"{url}?{encoded_params}"

        request_data = None
        headers = {}
        if form_data is not None:
            request_data = urllib.parse.urlencode(form_data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(
            url,
            data=request_data,
            method=method,
            headers=headers,
        )

        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and allow_reauth and api_path != "/api/v2/auth/login":
                self.login(force=True)
                return self._request(
                    method,
                    api_path,
                    params=params,
                    form_data=form_data,
                    allow_reauth=False,
                )
            body = exc.read().decode("utf-8", errors="replace")
            raise QbittorrentClientError(f"HTTP {exc.code} from qBittorrent: {body}") from exc
        except urllib.error.URLError as exc:
            raise QbittorrentClientError(f"Unable to connect to qBittorrent: {exc.reason}") from exc

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
        if response != "Ok.":
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
