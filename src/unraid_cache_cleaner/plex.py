"""Minimal Plex Web API client for duplicate scanning."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .models import PlexSection

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 200


class PlexClientError(RuntimeError):
    """Raised when Plex cannot be queried safely."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PlexClient:
    """Small token-authenticated client for the Plex Web API.

    Read-only: it only issues GETs against the library endpoints. The token is
    sent as an ``X-Plex-Token`` header (never in the URL query, so it stays out
    of request logs) and JSON is requested via ``Accept: application/json``.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = 30,
        verify_tls: bool = True,
    ) -> None:
        if not base_url or not token:
            raise PlexClientError("PLEX_URL and PLEX_TOKEN are required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = []

        if self.base_url.startswith("https://"):
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))

        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = [
            ("X-Plex-Token", self.token),
            ("Accept", "application/json"),
            ("User-Agent", "unraid-cache-cleaner/0.1.0"),
        ]
        return opener

    def _request(
        self,
        api_path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> str:
        encoded_params = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{api_path}"
        if encoded_params:
            url = f"{url}?{encoded_params}"

        request = urllib.request.Request(
            url,
            method="GET",
            headers=dict(extra_headers or {}),
        )

        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise PlexClientError(
                    "Plex rejected the token (401). Re-copy X-Plex-Token.",
                    status_code=401,
                ) from exc
            body = exc.read().decode("utf-8", errors="replace")
            raise PlexClientError(f"HTTP {exc.code} from Plex: {body}", status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise PlexClientError(
                f"Unable to connect to Plex at {self.base_url}: {exc.reason}"
            ) from exc

    def _get_json(
        self,
        api_path: str,
        params: Optional[Dict[str, str]] = None,
        *,
        container_start: Optional[int] = None,
        container_size: Optional[int] = None,
    ) -> dict:
        extra_headers: Dict[str, str] = {}
        if container_start is not None:
            extra_headers["X-Plex-Container-Start"] = str(container_start)
        if container_size is not None:
            extra_headers["X-Plex-Container-Size"] = str(container_size)

        body = self._request(api_path, params=params, extra_headers=extra_headers)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise PlexClientError(f"Plex returned invalid JSON from {api_path}: {exc}") from exc

    def fetch_sections(self) -> List[PlexSection]:
        """Return the Plex library sections."""

        payload = self._get_json("/library/sections")
        container = payload.get("MediaContainer", {})
        sections: List[PlexSection] = []
        for directory in container.get("Directory", []):
            sections.append(
                PlexSection(
                    key=str(directory.get("key", "")),
                    type=str(directory.get("type", "")),
                    title=str(directory.get("title", "")),
                )
            )
        return sections

    def fetch_duplicates(
        self,
        section_id: str,
        item_type: int,
        *,
        page_size: int = _PAGE_SIZE,
    ) -> List[dict]:
        """Return the raw duplicate ``Metadata`` items for one section.

        Pages through the section using ``X-Plex-Container-Start`` /
        ``X-Plex-Container-Size`` until ``MediaContainer.totalSize`` is reached.
        An unknown section (HTTP 404) is skipped with a warning and yields ``[]``
        rather than crashing; a rejected token (401) still propagates.
        """

        params = {
            "type": str(item_type),
            "duplicate": "1",
            "includeGuids": "1",
        }
        items: List[dict] = []
        start = 0
        while True:
            try:
                payload = self._get_json(
                    f"/library/sections/{section_id}/all",
                    params,
                    container_start=start,
                    container_size=page_size,
                )
            except PlexClientError as exc:
                if exc.status_code == 404:
                    LOGGER.warning("Plex section %s not found (404); skipping", section_id)
                    return []
                raise

            container = payload.get("MediaContainer", {})
            page = container.get("Metadata", [])
            items.extend(page)

            total = int(container.get("totalSize", container.get("size", len(page))))
            start += page_size
            if not page or start >= total:
                break

        return items
