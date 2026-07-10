"""Action layer for the Plex duplicate web GUI (#34, Phase 2).

This is the project's **first outside-triggered mutation of media**. Every other
surface is read-only by construction — the Plex/``*arr`` clients only GET, and the
cleanup deleter only ever touches qBittorrent orphans under ``/data``. A reclaim
here deletes *library* media, so the whole module is the **safety envelope around
the delete**, not a thin CRUD endpoint. :class:`ReclaimService` owns that envelope
and is constructed with injected collaborators (report provider, filesystem
deleter, Radarr/Sonarr clients, audit sink, clock) so every path is unit-testable
with fakes and no real socket or disk write.

Fail-closed on every axis:

* **Disabled by default** — ``WEB_ENABLE_ACTIONS=false`` makes a reclaim request a
  ``403`` that never reaches any backend.
* **Dry-run by default** — ``WEB_ACTIONS_DRY_RUN=true`` reports the would-delete set
  and calls no deleter.
* **Token-gated** — a shared ``WEB_ACTION_TOKEN`` must be presented; enabling
  actions *without* a token is itself refused, so an unauthenticated mutation
  surface can never be exposed on ``0.0.0.0``.
* **Fresh server-side truth** — targets are resolved only against a freshly read
  report snapshot; a client's own path/association/size/backend is never trusted,
  and a ``generated_at`` mismatch (a page built on a stale report) is a ``409``.
* **Honors every safety signal the report already computes** — the ``keeper`` is
  never deleted, a ``mismatch`` group is never reclaimed, an ``unknown``
  association is never auto-deleted, and a group with no authoritative keeper is
  not actionable.
* **Serialized** — one lock spans snapshot → validate → delete → audit, so two
  browser tabs cannot both reclaim the same group.
* **Routed by association** — an ``untracked`` copy is a filesystem delete (only
  when a path map resolves the Plex path to a *mounted* container file), a
  ``tracked`` copy is a Radarr/Sonarr ``DELETE`` (so it does not re-download).
* **Re-validated immediately before the delete (TOCTOU)** — a filesystem target is
  re-``lstat``'d for a matching size and a regular (non-symlink) file under the
  media root; a tracked target's ``*arr`` file id is resolved live and refused if
  it is missing or ambiguous.
* **Stacked-safe** — a logical copy's parts are *all* prevalidated before *any* is
  deleted, so a multi-part copy is removed whole or refused whole.
"""

from __future__ import annotations

import hmac
import logging
import os
import stat as stat_mod
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import arr, dedupe
from .arr import ArrClientError
from .config import Config
from .models import ActionRecord
from .planner import is_within

LOGGER = logging.getLogger(__name__)

#: A report provider returns the current report payload dict, or ``None`` when no
#: usable report exists yet. Structurally identical to ``web.ReportProvider`` but
#: declared here to avoid importing ``web`` (which imports this module).
ReportProvider = Callable[[], Optional[dict]]

#: A filesystem deleter removes one already-validated container path. Injected so a
#: test can record calls without touching disk; production passes ``os.unlink``.
FilesystemDeleter = Callable[[Path], None]

#: An audit sink persists the action records for a completed reclaim (real deletes
#: and delete failures only — a dry-run or a refusal touches nothing to audit).
#: Shaped exactly like ``StateStore.record_actions`` so the store method can be
#: passed directly; the timestamp comes from the service's injected clock.
AuditSink = Callable[[List[ActionRecord], float], None]

STATUS_DELETED = "deleted"
STATUS_WOULD_DELETE = "would-delete"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"

BACKEND_FILESYSTEM = "filesystem"

_MISMATCH = dedupe.MISMATCH


@dataclass(frozen=True)
class ReclaimTarget:
    """One operator-selected physical file to reclaim: ``{rating_key, part_id}``.

    This is the *only* client-supplied input trusted, and only as a lookup key —
    every attribute of what gets deleted (path, size, association, backend) is read
    from the fresh server-side report, never from the request.
    """

    rating_key: str
    part_id: int


@dataclass(frozen=True)
class ReclaimResult:
    """The per-target outcome, surfaced to the operator and (for real deletes and
    failures) mirrored into the audit trail."""

    rating_key: str
    part_id: int
    status: str
    backend: str
    message: str
    reclaimed_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "rating_key": self.rating_key,
            "part_id": self.part_id,
            "status": self.status,
            "backend": self.backend,
            "message": self.message,
            "reclaimed_bytes": self.reclaimed_bytes,
        }


@dataclass(frozen=True)
class ReclaimResponse:
    """The whole-request outcome. ``status_code`` is the HTTP status the web layer
    returns; ``results`` is the per-target detail (empty on a gate refusal)."""

    status_code: int
    enabled: bool
    dry_run: bool
    message: str
    results: List[ReclaimResult]

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "message": self.message,
            "results": [result.as_dict() for result in self.results],
        }


@dataclass(frozen=True)
class _Part:
    part_id: int
    plex_path: Path
    size: int


@dataclass(frozen=True)
class _CopyEntry:
    """A resolved reclaim candidate: one logical copy plus the safety signals that
    decide whether — and how — it may be deleted."""

    rating_key: str
    kind: str
    classification: str
    is_keeper: bool
    group_has_keeper: bool
    association: str
    arr_tracked: Optional[str]
    parts: Tuple[_Part, ...]


class _ActionIndex:
    """Maps every addressable ``{rating_key, part_id}`` in the current report to its
    logical copy, and records the identities that are ambiguous (the same key seen
    twice) so those are refused rather than acted on."""

    def __init__(self) -> None:
        self.entries: Dict[Tuple[str, int], _CopyEntry] = {}
        self.ambiguous: Set[Tuple[str, int]] = set()

    def lookup(self, target: ReclaimTarget) -> Tuple[Optional[_CopyEntry], Optional[str]]:
        key = (target.rating_key, target.part_id)
        if key in self.ambiguous:
            return None, (
                "ambiguous target: the report lists more than one copy with this "
                "rating_key + part_id"
            )
        entry = self.entries.get(key)
        if entry is None:
            return None, "target not found in the current report"
        return entry, None


def build_action_index(payload: dict) -> _ActionIndex:
    """Index the report payload for action lookup, honoring the report's identity
    rules (a group with no ``rating_key`` and a part with ``part_id == 0`` are
    unaddressable, per Plex's ``""``/``0`` fallbacks) and flagging any duplicate
    identity as ambiguous."""

    index = _ActionIndex()
    groups = payload.get("groups") or []
    if not isinstance(groups, list):
        return index
    for group in groups:
        if not isinstance(group, dict):
            continue
        rating_key = group.get("rating_key")
        # A missing Plex ratingKey serializes as "" — unaddressable, so the whole
        # group is skipped (never guessed at).
        if not isinstance(rating_key, str) or rating_key == "":
            continue
        keeper = group.get("keeper") if isinstance(group.get("keeper"), dict) else None
        classification = str(group.get("classification") or "")
        kind = str(group.get("kind") or "")
        copies = group.get("copies") or []
        if not isinstance(copies, list):
            continue
        for copy in copies:
            if not isinstance(copy, dict):
                continue
            entry = _CopyEntry(
                rating_key=rating_key,
                kind=kind,
                classification=classification,
                is_keeper=_is_keeper(copy, keeper),
                group_has_keeper=keeper is not None,
                association=str(copy.get("association") or arr.UNKNOWN),
                arr_tracked=copy.get("arr_tracked"),
                parts=_copy_parts(copy),
            )
            for part in entry.parts:
                # A part_id of 0 (Plex omitted the Part id) is unaddressable — it
                # can never be a target key, though it is still a member of the
                # copy's deletion set when a sibling part is targeted; a degenerate
                # part is caught again at reclaim time and refuses the whole copy.
                if part.part_id == 0:
                    continue
                key = (rating_key, part.part_id)
                if key in index.entries or key in index.ambiguous:
                    index.ambiguous.add(key)
                    index.entries.pop(key, None)
                    continue
                index.entries[key] = entry
    return index


def _copy_parts(copy: dict) -> Tuple[_Part, ...]:
    """Every physical file backing a logical copy, as ``_Part`` records.

    Reads the always-present ``parts`` array (#17), falling back to the copy's own
    ``file``/``size`` for a pre-#34 report that predates per-part serialization.
    """

    raw_parts = copy.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raw_parts = [{"part_id": 0, "file": copy.get("file"), "size": copy.get("size")}]
    parts: List[_Part] = []
    for raw in raw_parts:
        if not isinstance(raw, dict):
            continue
        file_value = raw.get("file")
        parts.append(
            _Part(
                part_id=_as_int(raw.get("part_id")),
                plex_path=Path(str(file_value)) if file_value else Path(""),
                size=_as_int(raw.get("size")),
            )
        )
    return tuple(parts)


def _is_keeper(copy: dict, keeper: Optional[dict]) -> bool:
    """Identity match against the report's authoritative ``keeper`` (file + Plex
    ``media_id``), mirroring the read-only viewer so the two never disagree about
    which copy is protected."""

    if keeper is None:
        return False
    return copy.get("file") == keeper.get("file") and copy.get("media_id") == keeper.get(
        "media_id"
    )


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class ReclaimService:
    """Serialized, fail-closed reclaim of report-selected duplicate copies."""

    def __init__(
        self,
        config: Config,
        provider: ReportProvider,
        *,
        filesystem_deleter: FilesystemDeleter = os.unlink,
        radarr: Optional[object] = None,
        sonarr: Optional[object] = None,
        audit: Optional[AuditSink] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._provider = provider
        self._filesystem_deleter = filesystem_deleter
        self._radarr = radarr
        self._sonarr = sonarr
        self._audit = audit
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._config.web_actions_enabled)

    @property
    def dry_run(self) -> bool:
        return bool(self._config.web_actions_dry_run)

    def reclaim(
        self,
        targets: Sequence[ReclaimTarget],
        *,
        token: Optional[str],
        report_generated_at: Optional[object],
    ) -> ReclaimResponse:
        """Reclaim ``targets`` under the full safety envelope. Never raises for a
        bad request — every failure is a typed :class:`ReclaimResponse`."""

        if not self.enabled:
            return self._gate(403, "web actions are disabled (set WEB_ENABLE_ACTIONS=true)")

        # The lock spans the entire critical section — snapshot, validation, the
        # backend delete, and the audit write — so two concurrent POSTs can never
        # both reclaim the same group, and the audit connection (opened
        # check_same_thread=False) is only ever touched by one thread at a time.
        with self._lock:
            configured = self._config.web_action_token
            if not configured:
                # Enabled but unauthenticated: refuse rather than expose a mutation
                # endpoint with no gate on a LAN-bound socket.
                return self._gate(
                    403,
                    "web actions are enabled but WEB_ACTION_TOKEN is not set; refusing to "
                    "expose an unauthenticated delete endpoint",
                )
            if not _token_ok(token, configured):
                return self._gate(403, "invalid or missing action token")

            payload = self._load_report()
            if payload is None:
                return self._gate(409, "no duplicate report is available to act on")

            if not _generation_matches(report_generated_at, payload.get("generated_at")):
                return self._gate(
                    409,
                    "the duplicate report changed since this page loaded; reload and retry",
                )

            index = build_action_index(payload)
            results: List[ReclaimResult] = []
            seen: Set[Tuple[str, int]] = set()
            seen_copies: Set[int] = set()
            # One *arr file index per backend per request (built lazily), so a
            # multi-target reclaim resolves every selected basename against a single
            # fetch instead of one full-library fan-out per part per target.
            arr_index_cache: Dict[str, Dict[str, List[int]]] = {}
            for target in targets:
                key = (target.rating_key, target.part_id)
                if key in seen:  # a duplicate target is a no-op, not a double delete
                    continue
                seen.add(key)
                try:
                    result = self._reclaim_one(target, index, seen_copies, arr_index_cache)
                except Exception:  # noqa: BLE001 — one bad target must not crash the request
                    LOGGER.warning("reclaim of %s failed unexpectedly", key, exc_info=True)
                    result = self._refused(target, "", "internal error while processing this target")
                if result is not None:
                    results.append(result)

        self._log_summary(results)
        return ReclaimResponse(200, True, self.dry_run, "", results)

    # -- per-target ---------------------------------------------------------- #

    def _reclaim_one(
        self,
        target: ReclaimTarget,
        index: _ActionIndex,
        seen_copies: Set[int],
        arr_index_cache: Dict[str, Dict[str, List[int]]],
    ) -> Optional[ReclaimResult]:
        if not target.rating_key or target.part_id == 0:
            return self._refused(target, "", "invalid target id (empty rating_key or zero part_id)")

        entry, error = index.lookup(target)
        if error is not None or entry is None:
            return self._refused(target, "", error or "target not found")

        # A logical copy shares one entry object across its parts. If an earlier
        # target already reclaimed this copy (a different part of the same stacked
        # copy), skip the sibling rather than re-deleting or double-counting it.
        if id(entry) in seen_copies:
            return None
        seen_copies.add(id(entry))

        if not entry.group_has_keeper:
            return self._refused(target, "", "group has no authoritative keeper; not reclaimable")
        if entry.classification == _MISMATCH:
            return self._refused(
                target, "", "mismatch group: Plex merged different titles; never reclaimed"
            )
        if entry.is_keeper:
            return self._refused(target, "", "target is the group keeper; never deleted")
        if entry.association == arr.UNKNOWN:
            return self._refused(
                target, "", "association is unknown; never auto-deleted (verify by hand)"
            )
        if any(part.part_id == 0 or not str(part.plex_path) or str(part.plex_path) == "." for part in entry.parts):
            return self._refused(
                target, "", "copy has an unaddressable part; delete it manually"
            )

        if entry.association == arr.UNTRACKED:
            return self._reclaim_filesystem(target, entry)
        if entry.association == arr.TRACKED:
            return self._reclaim_arr(target, entry, arr_index_cache)
        return self._refused(target, "", f"unsupported association {entry.association!r}")

    # -- filesystem backend -------------------------------------------------- #

    def _reclaim_filesystem(self, target: ReclaimTarget, entry: _CopyEntry) -> ReclaimResult:
        # Prevalidate EVERY part before deleting ANY, so a stacked copy is removed
        # whole or refused whole (never half-deleted).
        validated: List[Tuple[Path, int]] = []
        for part in entry.parts:
            container_path, error = self._validate_fs_part(part)
            if error is not None or container_path is None:
                return self._refused(target, BACKEND_FILESYSTEM, error or "validation failed")
            validated.append((container_path, part.size))

        total = sum(size for _, size in validated)
        if self.dry_run:
            return self._would_delete(
                target, BACKEND_FILESYSTEM, total, f"{len(validated)} file(s) via filesystem"
            )

        records: List[ActionRecord] = []
        deleted_bytes = 0
        for container_path, size in validated:
            try:
                self._filesystem_deleter(container_path)
            except OSError as exc:
                records.append(
                    ActionRecord(
                        path=container_path,
                        action="web-reclaim:filesystem",
                        status=STATUS_ERROR,
                        size=size,
                        message=f"rating_key={target.rating_key} part_id={target.part_id}: {exc}",
                    )
                )
                self._flush_audit(records)
                return self._error(
                    target,
                    BACKEND_FILESYSTEM,
                    deleted_bytes,
                    f"partial: deleted {deleted_bytes} bytes, then failed on {container_path}: {exc}",
                )
            deleted_bytes += size
            records.append(
                ActionRecord(
                    path=container_path,
                    action="web-reclaim:filesystem",
                    status=STATUS_DELETED,
                    size=size,
                    message=f"rating_key={target.rating_key} part_id={target.part_id}",
                )
            )
        self._flush_audit(records)
        return self._deleted(target, BACKEND_FILESYSTEM, deleted_bytes, f"{len(validated)} file(s)")

    def _validate_fs_part(self, part: _Part) -> Tuple[Optional[Path], Optional[str]]:
        """Resolve a Plex path to a real, mounted, size-matching regular file under a
        configured media root — or return the refusal reason. Every check is
        fail-closed: an unmapped path, an unmounted root, a symlink, a directory, a
        missing file, or a size that no longer matches the report all refuse."""

        mapped = self._map_path(part.plex_path)
        if mapped is None:
            return None, (
                f"no WEB_MEDIA_PATH_MAP entry maps {part.plex_path} into this container; "
                "the Plex media library must be mounted and mapped for a filesystem delete"
            )
        container_path, container_prefix = mapped

        if not container_prefix.is_dir():
            return None, f"media root {container_prefix} is not mounted in this container"
        if part.size <= 0:
            return None, f"cannot validate a zero-size target: {container_path}"

        try:
            info = os.lstat(container_path)
        except OSError as exc:
            return None, f"target file not present: {container_path} ({exc.__class__.__name__})"
        if not stat_mod.S_ISREG(info.st_mode):
            return None, f"target is not a regular file (symlink or directory?): {container_path}"

        # Defense against a symlinked parent that would escape the media root: the
        # *real* path must still resolve inside the root's real path.
        real_path = Path(os.path.realpath(container_path))
        real_root = Path(os.path.realpath(container_prefix))
        if not is_within(real_path, real_root):
            return None, f"resolved path escapes the media root: {container_path}"

        if info.st_size != part.size:
            return None, (
                f"size changed since the report ({info.st_size} on disk != {part.size} reported); "
                "refusing a stale target"
            )
        return container_path, None

    def _map_path(self, plex_path: Path) -> Optional[Tuple[Path, Path]]:
        """Longest-prefix, component-aware map of a Plex path to ``(container_path,
        container_prefix)``. Component-aware so ``/mnt/user/Media`` never matches
        ``/mnt/user/Media2``; a ``..`` in the remainder is refused (never joined)."""

        plex_parts = plex_path.parts
        best: Optional[Tuple[int, Path, Path]] = None
        for plex_prefix, container_prefix in self._config.web_media_path_map:
            prefix_parts = plex_prefix.parts
            if len(prefix_parts) > len(plex_parts):
                continue
            if tuple(plex_parts[: len(prefix_parts)]) != prefix_parts:
                continue
            remainder = plex_parts[len(prefix_parts):]
            if any(component == ".." for component in remainder):
                continue
            candidate = container_prefix.joinpath(*remainder)
            if best is None or len(prefix_parts) > best[0]:
                best = (len(prefix_parts), candidate, container_prefix)
        if best is None:
            return None
        return best[1], best[2]

    # -- *arr backend -------------------------------------------------------- #

    def _reclaim_arr(
        self,
        target: ReclaimTarget,
        entry: _CopyEntry,
        arr_index_cache: Dict[str, Dict[str, List[int]]],
    ) -> ReclaimResult:
        backend = entry.arr_tracked or ""
        client = self._arr_client(backend)
        if client is None:
            return self._refused(
                target, backend, f"{backend or 'arr'} client is not configured for actions"
            )

        # Build the *arr's basename -> file-id index once per request (cached per
        # backend), then resolve every part against it — a stale/ambiguous id is
        # refused before any DELETE (TOCTOU: the index is live *arr state).
        try:
            file_index = self._arr_file_index(backend, client, arr_index_cache)
        except ArrClientError as exc:
            return self._refused(target, backend, f"could not query {backend}: {exc}")

        resolved: List[Tuple[int, Path, int]] = []
        for part in entry.parts:
            basename = part.plex_path.name
            ids = file_index.get(basename, [])
            if not ids:
                return self._refused(
                    target, backend, f"no {backend} file matches {basename} (already removed?)"
                )
            if len(ids) > 1:
                return self._refused(
                    target, backend, f"ambiguous: {len(ids)} {backend} files match {basename}"
                )
            resolved.append((ids[0], part.plex_path, part.size))

        total = sum(size for _, _, size in resolved)
        if self.dry_run:
            return self._would_delete(target, backend, total, f"{len(resolved)} file(s) via {backend}")

        records: List[ActionRecord] = []
        deleted_bytes = 0
        for file_id, plex_path, size in resolved:
            try:
                self._delete_arr(client, backend, file_id)
            except ArrClientError as exc:
                records.append(
                    ActionRecord(
                        path=plex_path,
                        action=f"web-reclaim:{backend}",
                        status=STATUS_ERROR,
                        size=size,
                        message=f"id={file_id} rating_key={target.rating_key}: {exc}",
                    )
                )
                self._flush_audit(records)
                return self._error(
                    target,
                    backend,
                    deleted_bytes,
                    f"partial: {backend} delete failed for {plex_path.name}: {exc}",
                )
            deleted_bytes += size
            records.append(
                ActionRecord(
                    path=plex_path,
                    action=f"web-reclaim:{backend}",
                    status=STATUS_DELETED,
                    size=size,
                    message=f"id={file_id} rating_key={target.rating_key} part_id={target.part_id}",
                )
            )
        self._flush_audit(records)
        return self._deleted(target, backend, deleted_bytes, f"{len(resolved)} file(s) via {backend}")

    def _arr_client(self, backend: str) -> Optional[object]:
        if backend == arr.RADARR:
            return self._radarr
        if backend == arr.SONARR:
            return self._sonarr
        return None

    @staticmethod
    def _arr_file_index(
        backend: str, client: object, cache: Dict[str, Dict[str, List[int]]]
    ) -> Dict[str, List[int]]:
        """The backend's basename -> [file id] index, fetched at most once per
        request. May raise ``ArrClientError`` (fail-closed) on an *arr outage."""

        index = cache.get(backend)
        if index is None:
            index = client.fetch_file_index()  # type: ignore[attr-defined]
            cache[backend] = index
        return index

    @staticmethod
    def _delete_arr(client: object, backend: str, file_id: int) -> None:
        if backend == arr.RADARR:
            client.delete_movie_file(file_id)  # type: ignore[attr-defined]
        else:
            client.delete_episode_file(file_id)  # type: ignore[attr-defined]

    # -- helpers ------------------------------------------------------------- #

    def _load_report(self) -> Optional[dict]:
        try:
            payload = self._provider()
        except Exception:  # noqa: BLE001 — a broken provider degrades to "no report"
            LOGGER.warning("reading the duplicate report for an action failed", exc_info=True)
            return None
        return payload if isinstance(payload, dict) else None

    def _flush_audit(self, records: List[ActionRecord]) -> None:
        if not records or self._audit is None:
            return
        try:
            self._audit(records, self._clock())
        except Exception:  # noqa: BLE001 — an audit-write failure must not mask a completed delete
            LOGGER.warning("persisting the reclaim audit trail failed", exc_info=True)

    def _log_summary(self, results: List[ReclaimResult]) -> None:
        counts: Dict[str, int] = {}
        reclaimed = 0
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
            reclaimed += result.reclaimed_bytes
        LOGGER.info(
            "web reclaim: dry_run=%s targets=%s deleted=%s would_delete=%s refused=%s error=%s bytes=%s",
            self.dry_run,
            len(results),
            counts.get(STATUS_DELETED, 0),
            counts.get(STATUS_WOULD_DELETE, 0),
            counts.get(STATUS_REFUSED, 0),
            counts.get(STATUS_ERROR, 0),
            reclaimed,
        )

    def _gate(self, status_code: int, message: str) -> ReclaimResponse:
        return ReclaimResponse(status_code, self.enabled, self.dry_run, message, [])

    def _refused(self, target: ReclaimTarget, backend: str, message: str) -> ReclaimResult:
        return ReclaimResult(target.rating_key, target.part_id, STATUS_REFUSED, backend, message)

    def _would_delete(
        self, target: ReclaimTarget, backend: str, total: int, message: str
    ) -> ReclaimResult:
        return ReclaimResult(
            target.rating_key, target.part_id, STATUS_WOULD_DELETE, backend, message, total
        )

    def _deleted(
        self, target: ReclaimTarget, backend: str, total: int, message: str
    ) -> ReclaimResult:
        return ReclaimResult(
            target.rating_key, target.part_id, STATUS_DELETED, backend, message, total
        )

    def _error(
        self, target: ReclaimTarget, backend: str, total: int, message: str
    ) -> ReclaimResult:
        return ReclaimResult(
            target.rating_key, target.part_id, STATUS_ERROR, backend, message, total
        )


def _token_ok(supplied: Optional[str], configured: str) -> bool:
    """Constant-time token comparison (never leaks length via early exit).

    Compares UTF-8 bytes, not ``str``: ``hmac.compare_digest`` raises ``TypeError``
    on a ``str`` containing non-ASCII, which — since a client controls the token —
    would otherwise crash the request thread on a hostile token rather than simply
    refuse it.
    """

    if not supplied:
        return False
    return hmac.compare_digest(str(supplied).encode("utf-8"), configured.encode("utf-8"))


def _generation_matches(client_value: Optional[object], report_value: object) -> bool:
    """Whether the client's echoed ``generated_at`` identifies the current report.

    A missing client value, an unparseable one, or a value that does not equal the
    report's ``generated_at`` all fail — so a reclaim built on a stale (or absent)
    report snapshot is refused, defeating a replay of an old page's selection.
    """

    if client_value is None:
        return False
    try:
        return float(client_value) == float(report_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
