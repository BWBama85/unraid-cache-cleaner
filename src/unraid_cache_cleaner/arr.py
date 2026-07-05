"""Optional Radarr/Sonarr association layer.

Enriches a Plex duplicate report (#7) with whether each copy is tracked by
Radarr (movies) or Sonarr (TV) — so a redundant copy that an ``*arr`` tracks is
flagged as "delete via the ``*arr`` or it re-downloads", while an untracked
copy is confirmed safe. This is read-only enrichment: nothing here deletes,
moves, or unmonitors anything, and a missing/unreachable ``*arr`` degrades the
report to Plex-only rather than failing it.

Two thin ``urllib`` clients mirror :mod:`qbittorrent` / :mod:`plex` (custom
``ArrClientError``, TLS-verify toggle, timeouts, ``X-Api-Key`` header never in a
URL). :func:`annotate` is a pure transform over analyzed
:class:`~unraid_cache_cleaner.models.DuplicateGroup` records.

Join strategy differs by kind, because the two ``*arr`` id joins are not equally
reliable:

* **Movies (Radarr).** Plex's movie ``tmdb://`` guid is the same TMDB id Radarr
  keys on, so movies are *id-anchored*: within a group whose TMDB id Radarr
  tracks, the copy whose basename matches Radarr's file is ``tracked`` and the
  other redundant copies are ``untracked`` (safe). If the id is absent, not in
  Radarr, or no copy basename matches, every copy is ``unknown``.
* **Episodes (Sonarr).** Plex's episode ``Guid`` entries are *episode-level* ids,
  not the *series* TVDB id Sonarr keys on, so an id-anchored join is unreliable.
  Episodes instead match by basename against every tracked episode file: a copy
  whose basename Sonarr tracks is ``tracked``; any other copy is ``unknown``
  (never ``untracked``, so a TV copy is never falsely labeled safe).
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import USER_AGENT, dedupe
from .http_redirect import build_handler
from .models import DuplicateGroup, MediaCopy

TRACKED = "tracked"
UNTRACKED = "untracked"
UNKNOWN = "unknown"

RADARR = "radarr"
SONARR = "sonarr"


class ArrClientError(RuntimeError):
    """Raised when a Radarr/Sonarr instance cannot be queried safely."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ArrClient:
    """Shared ``urllib`` plumbing for the Radarr/Sonarr v3 APIs.

    Read-only: only issues GETs. The API key travels as an ``X-Api-Key`` header
    (never in the URL query, so it stays out of request logs) and JSON is
    requested via ``Accept: application/json``.
    """

    service_name = "arr"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: int = 30,
        verify_tls: bool = True,
    ) -> None:
        if not base_url or not api_key:
            raise ArrClientError(f"{self.service_name} URL and API key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            build_handler(
                self.base_url,
                service_name=self.service_name,
                error_factory=ArrClientError,
            ),
        ]

        if urllib.parse.urlparse(self.base_url).scheme == "https":
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))

        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = [
            ("X-Api-Key", self.api_key),
            ("Accept", "application/json"),
            ("User-Agent", USER_AGENT),
        ]
        return opener

    def _get_json(self, api_path: str, params: Optional[Dict[str, str]] = None) -> object:
        encoded_params = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{api_path}"
        if encoded_params:
            url = f"{url}?{encoded_params}"

        request = urllib.request.Request(url, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ArrClientError(
                    f"{self.service_name} rejected the API key (401). Re-copy the API key.",
                    status_code=401,
                ) from exc
            detail = exc.read().decode("utf-8", errors="replace")
            raise ArrClientError(
                f"HTTP {exc.code} from {self.service_name}: {detail}", status_code=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise ArrClientError(
                f"Unable to connect to {self.service_name} at {self.base_url}: {exc.reason}"
            ) from exc
        except OSError as exc:
            # A read-phase timeout (socket.timeout/TimeoutError) or dropped
            # connection is an OSError that urllib does NOT wrap in URLError, so it
            # would otherwise escape _build_arr_indexes' ArrClientError handler and
            # crash the read-only report instead of degrading gracefully.
            raise ArrClientError(
                f"Unable to reach {self.service_name} at {self.base_url}: {exc}"
            ) from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ArrClientError(
                f"{self.service_name} returned invalid JSON from {api_path}: {exc}"
            ) from exc


class RadarrClient(_ArrClient):
    """Radarr v3 client: TMDB id -> tracked movie-file basenames."""

    service_name = "Radarr"

    def fetch_tracked_index(self) -> Dict[str, Set[str]]:
        """Map each movie's TMDB id to the basename(s) of its tracked file.

        Movies Radarr has not imported (no ``movieFile``) are skipped: their id
        never anchors an association, so those copies stay ``unknown``.
        """

        movies = self._get_json("/api/v3/movie")
        index: Dict[str, Set[str]] = {}
        for movie in movies or []:
            if not isinstance(movie, dict):
                continue
            tmdb_id = movie.get("tmdbId")
            if not tmdb_id:
                continue
            movie_file = movie.get("movieFile") or {}
            path = movie_file.get("path") or movie_file.get("relativePath")
            if not path:
                continue
            index.setdefault(str(tmdb_id), set()).add(Path(str(path)).name)
        return index


class SonarrClient(_ArrClient):
    """Sonarr v3 client: the set of all tracked episode-file basenames.

    Sonarr keys series by TVDB id, but Plex's episode guids are episode-level,
    so this deliberately returns a flat basename set for a basename-only join
    rather than an id-keyed index (see the module docstring).
    """

    service_name = "Sonarr"

    def fetch_tracked_index(self) -> Set[str]:
        # N+1: one /series call plus one /episodefile call per series. Fine for a
        # manual, one-shot report against a LAN Sonarr; a library with hundreds of
        # series means hundreds of serial round-trips. Bulk retrieval is a
        # follow-up optimization, not needed for correctness.
        series_list = self._get_json("/api/v3/series")
        basenames: Set[str] = set()
        for series in series_list or []:
            if not isinstance(series, dict):
                continue
            series_id = series.get("id")
            if series_id is None:
                continue
            files = self._get_json("/api/v3/episodefile", {"seriesId": str(series_id)})
            for episode_file in files or []:
                if not isinstance(episode_file, dict):
                    continue
                path = episode_file.get("path")
                if path:
                    basenames.add(Path(str(path)).name)
        return basenames


def _refresh(group: DuplicateGroup, copies: tuple[MediaCopy, ...]) -> DuplicateGroup:
    """Swap in the annotated ``copies`` and re-point ``keeper`` at the annotated
    best copy. Annotation only sets association fields, so classification and the
    reclaimable math are unchanged and are kept as-is (no need for a full
    re-analysis) — only ``keeper`` must be re-derived so it, too, carries the
    association."""

    ranked = dedupe.rank_copies(replace(group, copies=copies))
    return replace(group, copies=copies, keeper=ranked[0] if ranked else None)


def _stack_key(copy: MediaCopy, index: int):
    """Group parts into logical copies exactly as ``dedupe._merge_stacks`` does.

    A non-zero ``media_id`` ties a stacked copy's parts together; ``0`` means
    ungrouped, so each such part stands alone (keyed by position, never shared).
    """

    return copy.media_id if copy.media_id != 0 else ("solo", index)


def _apply(
    group: DuplicateGroup, associations: List[tuple[str, Optional[str]]]
) -> DuplicateGroup:
    copies = tuple(
        replace(c, association=assoc, arr_tracked=name)
        for c, (assoc, name) in zip(group.copies, associations)
    )
    return _refresh(group, copies)


def _all_unknown(group: DuplicateGroup) -> DuplicateGroup:
    return _apply(group, [(UNKNOWN, None)] * len(group.copies))


def _match_stacks(
    group: DuplicateGroup, tracked_basenames: Set[str]
) -> Tuple[set, set]:
    """Partition the group's logical copies (stacks) by how their part basenames
    match the ``*arr``'s tracked files.

    Returns ``(tracked_stacks, ambiguous_stacks)``. The ``*arr`` tracks one exact
    path per item, but the index only knows basenames (to bridge mount-path
    differences), so a basename that matches parts in more than one stack cannot
    be pinned to a single copy — those stacks are *ambiguous*, not confidently
    tracked. A basename that matches exactly one stack marks it ``tracked``. A
    stack pinned by any unique basename stays tracked even if it also carries a
    shared one.
    """

    basename_stacks: Dict[str, set] = {}
    for i, copy in enumerate(group.copies):
        if copy.file.name in tracked_basenames:
            basename_stacks.setdefault(copy.file.name, set()).add(_stack_key(copy, i))

    tracked: set = set()
    ambiguous: set = set()
    for stacks in basename_stacks.values():
        if len(stacks) == 1:
            tracked |= stacks
        else:
            ambiguous |= stacks
    ambiguous -= tracked
    return tracked, ambiguous


def _annotate_by_id(
    group: DuplicateGroup,
    index: Dict[str, Set[str]],
    namespace: str,
    arr_name: str,
) -> DuplicateGroup:
    plex_id = group.external_ids.get(namespace)
    tracked_basenames = index.get(plex_id) if plex_id else None

    # Id absent in Plex, or id not in the *arr: never claim safe.
    if not tracked_basenames:
        return _all_unknown(group)

    tracked_stacks, ambiguous_stacks = _match_stacks(group, tracked_basenames)
    if not tracked_stacks and not ambiguous_stacks:
        # Id matched but no basename matches (mount map ambiguous): all unknown.
        return _all_unknown(group)

    # A stacked copy is one logical unit: a stack the *arr uniquely tracks is
    # tracked (deleting it re-downloads); a stack whose only match is a basename
    # shared with another copy is unknown (can't tell which is the real file);
    # a stack matching nothing is the redundant, safe-to-delete copy.
    associations: List[tuple[str, Optional[str]]] = []
    for i, copy in enumerate(group.copies):
        key = _stack_key(copy, i)
        if key in tracked_stacks:
            associations.append((TRACKED, arr_name))
        elif key in ambiguous_stacks:
            associations.append((UNKNOWN, None))
        else:
            associations.append((UNTRACKED, None))
    return _apply(group, associations)


def _annotate_by_basename(
    group: DuplicateGroup,
    tracked_basenames: Set[str],
    arr_name: str,
) -> DuplicateGroup:
    tracked_stacks, _ = _match_stacks(group, tracked_basenames)
    # No reliable id anchor for episodes, so anything not uniquely tracked —
    # ambiguous or unmatched — is ``unknown`` (never ``untracked``/safe).
    associations = [
        (TRACKED, arr_name) if _stack_key(c, i) in tracked_stacks else (UNKNOWN, None)
        for i, c in enumerate(group.copies)
    ]
    return _apply(group, associations)


def annotate(
    groups: List[DuplicateGroup],
    radarr_index: Dict[str, Set[str]],
    sonarr_basenames: Set[str],
) -> List[DuplicateGroup]:
    """Return ``groups`` with each copy's association filled in.

    ``radarr_index`` maps TMDB id -> tracked movie-file basenames;
    ``sonarr_basenames`` is the flat set of tracked episode-file basenames. An
    empty index/set (e.g. that ``*arr`` unconfigured or unreachable) leaves the
    relevant kind ``unknown``. ``mismatch`` groups (Plex merged different titles)
    and non-movie/episode kinds are never labeled tracked/untracked — their
    copies keep the default ``unknown``, so a copy we don't trust the grouping of
    is never presented as safe.
    """

    out: List[DuplicateGroup] = []
    for group in groups:
        if group.classification == dedupe.MISMATCH:
            out.append(group)
        elif group.kind == "movie":
            out.append(_annotate_by_id(group, radarr_index, "tmdb", RADARR))
        elif group.kind == "episode":
            out.append(_annotate_by_basename(group, sonarr_basenames, SONARR))
        else:
            out.append(group)
    return out
