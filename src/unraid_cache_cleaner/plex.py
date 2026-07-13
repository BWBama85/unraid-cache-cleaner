"""Minimal Plex Web API client for duplicate scanning."""

from __future__ import annotations

import logging
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .http_client import JsonHttpClient
from .models import DuplicateGroup, MediaCopy, PlexSection

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 200


class PlexClientError(RuntimeError):
    """Raised when Plex cannot be queried safely."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PlexClient(JsonHttpClient):
    """Small token-authenticated client for the Plex Web API.

    Read-only: it only issues GETs against the library endpoints. The token is
    sent as an ``X-Plex-Token`` header (never in the URL query, so it stays out
    of request logs) and JSON is requested via ``Accept: application/json``. The
    ``urllib`` plumbing (fail-closed opener, transport taxonomy, JSON decode)
    comes from :class:`~unraid_cache_cleaner.http_client.JsonHttpClient`.
    """

    service_name = "Plex"
    error_class = PlexClientError

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = 30,
        verify_tls: bool = True,
        max_attempts: int = 1,
    ) -> None:
        if not base_url or not token:
            raise PlexClientError("PLEX_URL and PLEX_TOKEN are required")
        self.token = token
        super().__init__(
            base_url,
            timeout_seconds=timeout_seconds,
            verify_tls=verify_tls,
            max_attempts=max_attempts,
        )

    def _auth_headers(self) -> Sequence[Tuple[str, str]]:
        return (("X-Plex-Token", self.token), ("Accept", "application/json"))

    def _on_http_error(self, exc: urllib.error.HTTPError) -> Exception:
        if exc.code == 401:
            return PlexClientError(
                "Plex rejected the token (401). Re-copy X-Plex-Token.",
                status_code=401,
            )
        return super()._on_http_error(exc)

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

        # Every Plex endpoint returns a ``{"MediaContainer": ...}`` object; a
        # top-level array (or other non-object) would make the callers'
        # ``.get("MediaContainer", {})`` raise a bare AttributeError that the 404
        # handler in fetch_duplicates would not catch. Enforce the object shape
        # here so it surfaces as PlexClientError naming the endpoint instead.
        payload = super()._get_json(api_path, params, extra_headers=extra_headers)
        return self._ensure_json_object(payload, api_path)

    def _media_container(self, payload: dict, api_path: str) -> dict:
        """Return the ``MediaContainer`` object from a decoded Plex body.

        ``_ensure_json_object`` (in :meth:`_get_json`) guards only the *top level*.
        This guards the nested shape: a well-formed object whose ``MediaContainer``
        *value* is not a dict (``{"MediaContainer": []}`` / ``{"MediaContainer":
        null}``, e.g. from a misbehaving reverse proxy) would otherwise let the
        callers' ``container.get(...)`` raise a bare ``AttributeError`` that escapes
        the 404/section-skip handler and crashes the read-only report. A missing
        key stays ``{}`` (the prior behavior — "no items"); a present-but-non-dict
        value surfaces as ``PlexClientError`` naming the endpoint.
        """

        container = payload.get("MediaContainer", {})
        if not isinstance(container, dict):
            raise PlexClientError(
                f"Plex returned a non-object MediaContainer from {api_path} "
                f"(expected an object, got {type(container).__name__})"
            )
        return container

    def _container_list(self, container: dict, field: str, api_path: str) -> list:
        """Return a list-valued ``MediaContainer`` child (``Directory`` /
        ``Metadata``). Absent stays ``[]``; a present-but-non-list value surfaces
        as ``PlexClientError`` naming the endpoint, rather than crashing the
        subsequent ``for`` on a non-iterable (or silently mis-iterating a dict)."""

        value = container.get(field)
        if value is None:
            return []
        if not isinstance(value, list):
            raise PlexClientError(
                f"Plex returned a non-list {field} from {api_path} "
                f"(expected a list, got {type(value).__name__})"
            )
        return value

    def fetch_sections(self) -> List[PlexSection]:
        """Return the Plex library sections."""

        api_path = "/library/sections"
        payload = self._get_json(api_path)
        container = self._media_container(payload, api_path)
        sections: List[PlexSection] = []
        for directory in self._container_list(container, "Directory", api_path):
            if not isinstance(directory, dict):
                continue
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
        api_path = f"/library/sections/{section_id}/all"
        items: List[dict] = []
        start = 0
        while True:
            try:
                payload = self._get_json(
                    api_path,
                    params,
                    container_start=start,
                    container_size=page_size,
                )
            except PlexClientError as exc:
                if exc.status_code == 404:
                    LOGGER.warning("Plex section %s not found (404); skipping", section_id)
                    return []
                raise

            container = self._media_container(payload, api_path)
            page = self._container_list(container, "Metadata", api_path)
            items.extend(item for item in page if isinstance(item, dict))
            if not page:
                break

            # Advance by the number of items actually returned, not the requested
            # page_size: a server that caps the page below page_size would
            # otherwise make us skip the items in between (and one that ignores
            # paging entirely would double-count them). totalSize, when present,
            # bounds the loop so we avoid a trailing empty request.
            start += len(page)
            total = container.get("totalSize")
            if total is not None and start >= int(total):
                break

        return items


def _as_int(value: object) -> int:
    """Coerce a Plex numeric field to ``int``; ``None``/garbage -> ``0``."""

    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_opt_int(value: object) -> Optional[int]:
    """Coerce an optional Plex numeric field to ``int`` or ``None``."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _group_title(item: dict, kind: str, season: Optional[int], episode: Optional[int]) -> str:
    """Human-friendly title. Episodes prefix the show and ``SxxEyy`` marker."""

    title = str(item.get("title", "") or "")
    if kind != "episode":
        return title
    show = str(item.get("grandparentTitle", "") or "")
    marker = ""
    if season is not None and episode is not None:
        marker = f"S{season:02d}E{episode:02d}"
    parts = [part for part in (show, marker, title) if part]
    return " - ".join(parts) if parts else title


def build_duplicate_group(item: dict, kind: str) -> Optional[DuplicateGroup]:
    """Parse one raw Plex ``Metadata`` item into a :class:`DuplicateGroup`.

    Walks ``Media -> Part`` into :class:`MediaCopy` records — one per ``Part``,
    with every part of the same ``Media`` sharing its ``id`` as ``media_id`` so
    the dedupe engine merges a stacked copy rather than counting its parts as
    duplicates. ``Guid`` entries (from ``includeGuids=1``) become ``external_ids``.
    A part without a ``file`` is skipped; an item that yields no parts returns
    ``None`` so callers can drop it. Missing numeric fields default to ``0`` and
    a missing resolution to ``""`` — the dedupe engine already handles both.
    """

    copies: List[MediaCopy] = []
    for media_index, media in enumerate(item.get("Media") or []):
        # A ``Media`` element normally carries a non-zero ``id`` that ties its
        # parts together as one logical copy. If Plex omits it, fall back to a
        # per-element negative id so this element's parts still merge (and stay
        # distinct from other elements) instead of collapsing onto the ``0``
        # sentinel the dedupe engine reads as "ungrouped, stands alone" — which
        # would misread a stacked multi-part copy as separate duplicates.
        raw_id = _as_int(media.get("id"))
        media_id = raw_id if raw_id != 0 else -(media_index + 1)
        resolution = str(media.get("videoResolution", "") or "")
        bitrate = _as_int(media.get("bitrate"))
        codec = str(media.get("videoCodec", "") or "")
        container = str(media.get("container", "") or "")
        for part in media.get("Part") or []:
            file_path = part.get("file")
            if not file_path:
                continue
            copies.append(
                MediaCopy(
                    part_id=_as_int(part.get("id")),
                    file=Path(str(file_path)),
                    size=_as_int(part.get("size")),
                    resolution=resolution,
                    bitrate=bitrate,
                    codec=codec,
                    container=container,
                    media_id=media_id,
                )
            )
    if not copies:
        return None

    external_ids: Dict[str, str] = {}
    for guid in item.get("Guid") or []:
        scheme, _, value = str(guid.get("id", "")).partition("://")
        if scheme and value:
            external_ids.setdefault(scheme, value)

    season = _as_opt_int(item.get("parentIndex"))
    episode = _as_opt_int(item.get("index"))
    return DuplicateGroup(
        rating_key=str(item.get("ratingKey", "")),
        kind=kind,
        title=_group_title(item, kind, season, episode),
        copies=tuple(copies),
        year=_as_opt_int(item.get("year")),
        season=season,
        episode=episode,
        external_ids=external_ids,
    )
