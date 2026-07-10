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

import concurrent.futures
import logging
import urllib.error
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import dedupe
from .http_client import JsonHttpClient
from .models import DuplicateGroup, MediaCopy

LOGGER = logging.getLogger(__name__)

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


class _ArrClient(JsonHttpClient):
    """Shared ``urllib`` plumbing for the Radarr/Sonarr v3 APIs.

    Read-only: only issues GETs. The API key travels as an ``X-Api-Key`` header
    (never in the URL query, so it stays out of request logs) and JSON is
    requested via ``Accept: application/json``. The fail-closed opener, transport
    taxonomy (including the read-phase ``OSError`` wrapping so the report degrades
    rather than crashing), and JSON decode come from
    :class:`~unraid_cache_cleaner.http_client.JsonHttpClient`.
    """

    service_name = "arr"
    error_class = ArrClientError

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: int = 30,
        verify_tls: bool = True,
        max_attempts: int = 1,
    ) -> None:
        if not base_url or not api_key:
            raise ArrClientError(f"{self.service_name} URL and API key are required")
        self.api_key = api_key
        super().__init__(
            base_url,
            timeout_seconds=timeout_seconds,
            verify_tls=verify_tls,
            max_attempts=max_attempts,
        )

    def _auth_headers(self) -> Sequence[Tuple[str, str]]:
        return (("X-Api-Key", self.api_key), ("Accept", "application/json"))

    def _on_http_error(self, exc: urllib.error.HTTPError) -> Exception:
        if exc.code == 401:
            return ArrClientError(
                f"{self.service_name} rejected the API key (401). Re-copy the API key.",
                status_code=401,
            )
        return super()._on_http_error(exc)


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


#: Sonarr exposes episode files only per series — ``GET /api/v3/episodefile``
#: requires a ``seriesId`` (or explicit ``episodeFileIds``); an unfiltered call is
#: rejected — so the tracked-basename index costs one request per series. A
#: bounded thread pool turns those from hundreds of *serial* round-trips into a
#: handful of concurrent batches without opening a socket per series at once. The
#: bound (stdlib-only, 3.9-compatible) is the "explicit bound" the fan-out needs.
_SONARR_MAX_WORKERS = 8
#: Log a progress line every this many completed series (and once at the end), so
#: a large TV library shows the index advancing instead of looking hung.
_SONARR_PROGRESS_EVERY = 50


class SonarrClient(_ArrClient):
    """Sonarr v3 client: the set of all tracked episode-file basenames.

    Sonarr keys series by TVDB id, but Plex's episode guids are episode-level,
    so this deliberately returns a flat basename set for a basename-only join
    rather than an id-keyed index (see the module docstring).
    """

    service_name = "Sonarr"

    def fetch_tracked_index(self) -> Set[str]:
        """Return every tracked episode-file basename across the whole library.

        Sonarr has no bulk episode-file endpoint, so this fans one
        ``/api/v3/episodefile`` request out per series through a bounded
        :class:`~concurrent.futures.ThreadPoolExecutor`. Each worker still calls
        :meth:`_get_json`, so the fail-closed opener, timeout, redirect guard, and
        header-only API key all still apply; the opener carries no per-request
        mutable state (no cookie jar), so concurrent GETs are safe. A single
        worker failure aborts the whole index — the ``*ClientError`` propagates
        and the report degrades that kind to ``unknown`` — rather than returning a
        partial, misleading set.
        """

        series_list = self._get_json("/api/v3/series")
        series_ids = [
            series["id"]
            for series in series_list or []
            if isinstance(series, dict) and series.get("id") is not None
        ]
        if not series_ids:
            return set()

        total = len(series_ids)
        basenames: Set[str] = set()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_SONARR_MAX_WORKERS, total)
        ) as pool:
            futures = [
                pool.submit(self._episode_file_basenames, series_id)
                for series_id in series_ids
            ]
            try:
                for done, future in enumerate(
                    concurrent.futures.as_completed(futures), start=1
                ):
                    basenames |= future.result()
                    if done % _SONARR_PROGRESS_EVERY == 0 or done == total:
                        LOGGER.info("Sonarr: indexed %s/%s series", done, total)
            finally:
                # Any worker failure voids the whole index, so drop the requests
                # that have not started rather than fanning out the rest (already
                # running ones still finish under the pool's shutdown). A no-op on
                # the success path, where every future is already done — and robust
                # to a worker raising something other than ArrClientError.
                for pending in futures:
                    pending.cancel()
        return basenames

    def _episode_file_basenames(self, series_id: object) -> Set[str]:
        """Tracked episode-file basenames for one series (one ``/episodefile`` GET)."""

        files = self._get_json("/api/v3/episodefile", {"seriesId": str(series_id)})
        names: Set[str] = set()
        for episode_file in files or []:
            if not isinstance(episode_file, dict):
                continue
            path = episode_file.get("path")
            if path:
                names.add(Path(str(path)).name)
        return names


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
