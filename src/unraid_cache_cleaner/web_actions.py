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
  media root; a tracked target's ``*arr`` file id (serialized into the report by the
  arr layer, #61) is re-validated by a single by-id GET and refused on a 404 or if
  the current file's basename *or* size no longer matches the report's (``*arr``-side
  drift / an id reused for a different same-named file) — no full-library fan-out, so
  a tracked reclaim costs at most one GET + one DELETE per part.
* **Stacked-safe** — a logical copy's parts are *all* prevalidated before *any* is
  deleted, so a multi-part copy is removed whole or refused whole. For a filesystem
  copy this is enforced transactionally (#64): every part is first staged aside (an
  in-place rename to a ``*.uncc-reclaim`` sibling — same directory, so the rename is
  atomic and cannot fail cross-device), and only once *all* parts stage does the
  unlink pass run; a failure to stage any part rolls the already-staged parts back,
  leaving the copy intact with nothing deleted. The guarantee covers in-process
  failures (``EROFS``/``EACCES`` surface at the rename, where rollback is clean); the
  two out-of-process residues a rollback cannot reach — a crash mid-move and a
  post-commit purge failure — are reconciled by the startup staging sweep
  (:meth:`ReclaimService.reconcile_staging`, #72). A
  ``*arr`` reclaim is *not* transactional — a Radarr/Sonarr ``DELETE`` cannot be
  rolled back — so its parts are prevalidated whole but deleted independently, and a
  mid-delete failure is surfaced as a ``partial`` error (audited), not rolled back.
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
from .planner import collapse_roots, is_within

LOGGER = logging.getLogger(__name__)

#: A report provider returns the current report payload dict, or ``None`` when no
#: usable report exists yet. Structurally identical to ``web.ReportProvider`` but
#: declared here to avoid importing ``web`` (which imports this module).
ReportProvider = Callable[[], Optional[dict]]

#: A filesystem deleter removes one already-validated container path. Injected so a
#: test can record calls without touching disk; production passes ``os.unlink``. In
#: the two-phase filesystem reclaim (#64) this unlinks the *staged* path.
FilesystemDeleter = Callable[[Path], None]

#: A filesystem mover renames ``src`` → ``dst`` (production: ``os.rename``). Used to
#: stage a part aside before deleting it and to roll a staged part back on failure
#: (#64); injected so a test can drive a mid-stage rename failure.
FilesystemMover = Callable[[Path, Path], None]

#: An audit sink persists the action records for a completed reclaim (real deletes
#: and delete failures only — a dry-run or a refusal touches nothing to audit).
#: Shaped exactly like ``StateStore.record_actions`` so the store method can be
#: passed directly; the timestamp comes from the service's injected clock.
AuditSink = Callable[[List[ActionRecord], float], None]

STATUS_DELETED = "deleted"
STATUS_WOULD_DELETE = "would-delete"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"

#: Statuses the startup staging-reconciliation sweep (#72) writes to the ``actions``
#: audit trail — a crash-staged file brought back (``restored``), a completed-delete
#: leftover cleaned up (``removed``), or an ambiguous sibling left untouched
#: (``skipped``: a truncated/unreconstructable name, a symlink, or an ``OSError``).
STATUS_RESTORED = "restored"
STATUS_REMOVED = "removed"
STATUS_SKIPPED = "skipped"

BACKEND_FILESYSTEM = "filesystem"

#: Audit ``action`` for the reconciliation sweep, distinct from the reclaim actions
#: (``web-reclaim:filesystem`` / ``web-reclaim:<arr>``) so the ``/actions`` history
#: can tell an operator-triggered delete from an automatic staging cleanup.
RECONCILE_ACTION = "web-reclaim:reconcile"

#: Suffix appended in-place (same directory → same filesystem, so the rename is
#: atomic and never hits ``EXDEV``) to stage a filesystem part aside before it is
#: unlinked (#64). A leftover ``*.uncc-reclaim`` sibling means an earlier reclaim was
#: interrupted mid-flight; staging refuses to clobber it rather than guess.
STAGING_SUFFIX = ".uncc-reclaim"

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
    #: The ``*arr`` ``movieFile``/``episodeFile`` id backing this file, serialized
    #: into the report by the arr layer (#61) so a tracked reclaim deletes by id —
    #: re-validated with a single by-id GET immediately before the DELETE. ``None``
    #: on a non-tracked part, or a report predating the field (which refuses the
    #: tracked reclaim until regenerated, rather than resolving the id live).
    arr_file_id: Optional[int] = None


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
    #: The physical file paths the group's keeper occupies. A non-keeper copy whose
    #: parts overlap this set points at a file the keeper also uses (Plex can report
    #: one physical file under two Media/Part ids), so deleting it would destroy the
    #: keeper's file — such a target is refused even though it is not the keeper by
    #: identity.
    keeper_paths: frozenset = frozenset()


@dataclass(frozen=True)
class _DeleteJob:
    """One prevalidated physical delete, plus the audit text the shared delete loop
    (:meth:`ReclaimService._execute_deletes`) records for it.

    ``perform`` runs the backend delete (and may raise the backend's exception);
    ``audit_path`` is always the *original media path* (never a temporary staging
    path), so the ``actions`` history reads the same regardless of how the delete is
    carried out. The two message hooks are callables because their text depends on
    the raised exception (and, for the partial summary, the running deleted-byte
    total)."""

    audit_path: Path
    size: int
    perform: Callable[[], None]
    #: ``ActionRecord.message`` for the ``deleted`` row (exception-independent).
    deleted_message: str
    #: ``ActionRecord.message`` for the ``error`` row, given the raised exception.
    error_message: Callable[[BaseException], str]
    #: The :class:`ReclaimResult` ``error`` message, given the exception and the
    #: bytes already freed before this part failed.
    partial_message: Callable[[BaseException, int], str]
    #: The two-phase filesystem staging sibling this job unlinks, or ``None`` for a
    #: backend that does not stage (``*arr``). When a *post-commit* purge failure
    #: aborts the unlink pass, every not-yet-purged part — the one that failed *and*
    #: the un-attempted tail — is left at its staging sibling; :meth:`_execute_deletes`
    #: uses this to audit each leftover as a "left staged" row (#72), so the startup
    #: reconciliation sweep's job is fully discoverable in the audit trail.
    staged_path: Optional[Path] = None
    #: ``ActionRecord.message`` for a "left staged" leftover row, given the staged
    #: path. Set together with ``staged_path`` on a staged (filesystem) job.
    leftover_message: Optional[Callable[[Path], str]] = None


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
        keeper_paths = _keeper_part_paths(keeper)
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
                keeper_paths=keeper_paths,
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
                arr_file_id=_as_opt_int(raw.get("arr_file_id")),
            )
        )
    return tuple(parts)


def _keeper_part_paths(keeper: Optional[dict]) -> frozenset:
    """The set of physical file paths the keeper occupies (all of its parts).

    Used to refuse a non-keeper copy that shares any of these paths — the same
    physical file reported under a different ``media_id``/``part_id`` — so a reclaim
    can never delete the keeper's file by targeting a path-aliased sibling.
    """

    if keeper is None:
        return frozenset()
    return frozenset(
        part.plex_path for part in _copy_parts(keeper) if str(part.plex_path) not in ("", ".")
    )


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


def _as_opt_int(value: object) -> Optional[int]:
    """Parse a serialized ``*arr`` file id to a *positive* ``int``, or ``None``.

    Distinct from :func:`_as_int` (which returns ``0`` for the ``part_id``/``size``
    sentinels): a missing, ``null``, malformed, or non-positive ``arr_file_id`` must
    read as "no id" so the reclaim path refuses/falls through rather than deleting
    ``*arr`` file ``0``.
    """

    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@dataclass
class StagingSweepReport:
    """Outcome counts from one :meth:`ReclaimService.reconcile_staging` sweep.

    ``restored`` — crash-staged files brought back to their original path;
    ``removed`` — completed-delete leftovers cleaned up; ``would_remove`` — leftovers
    left in place because ``dry_run`` suppressed the delete; ``skipped`` — ambiguous
    siblings (unreconstructable name, symlink, or ``OSError``) left untouched."""

    restored: int = 0
    removed: int = 0
    would_remove: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.restored + self.removed + self.would_remove + self.skipped


class ReclaimService:
    """Serialized, fail-closed reclaim of report-selected duplicate copies."""

    def __init__(
        self,
        config: Config,
        provider: ReportProvider,
        *,
        filesystem_deleter: FilesystemDeleter = os.unlink,
        filesystem_mover: FilesystemMover = os.rename,
        radarr: Optional[object] = None,
        sonarr: Optional[object] = None,
        audit: Optional[AuditSink] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._provider = provider
        self._filesystem_deleter = filesystem_deleter
        self._filesystem_mover = filesystem_mover
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
            for target in targets:
                key = (target.rating_key, target.part_id)
                if key in seen:  # a duplicate target is a no-op, not a double delete
                    continue
                seen.add(key)
                try:
                    result = self._reclaim_one(target, index, seen_copies)
                except Exception:  # noqa: BLE001 — one bad target must not crash the request
                    LOGGER.warning("reclaim of %s failed unexpectedly", key, exc_info=True)
                    result = self._refused(target, "", "internal error while processing this target")
                if result is not None:
                    results.append(result)

        self._log_summary(results)
        return ReclaimResponse(200, True, self.dry_run, "", results)

    # -- staging reconciliation (#72) ---------------------------------------- #

    def reconcile_staging(self) -> StagingSweepReport:
        """Reconcile orphaned ``*.uncc-reclaim`` staging siblings under the configured
        media roots — the two-phase filesystem reclaim's (#64) only *out-of-process*
        residue, which its in-process rollback cannot reach.

        Two windows strand a sibling: a crash after a stage rename but before the
        unlink/rollback (the original is gone, sitting at its staging path), and a
        post-commit purge failure (an earlier unlink committed, so a later part can no
        longer be rolled back and stays staged). This sweep closes both, fail-closed:

        * **original missing** → restore the sibling to the original name — recovers
          the stranded media, and never clobbers (it runs only when the original is
          absent).
        * **original present** → remove the sibling — its media is already back at the
          original path, so the copy staged for deletion is redundant. Suppressed under
          ``dry_run`` (counted ``would_remove``); this is the sweep's only *deleting*
          action.
        * **ambiguous** — a name too long to have staged un-truncated (so the original
          can't be reconstructed), a symlink, or any ``OSError`` — is left untouched
          and audited ``skipped``; provenance is never guessed at.

        Held under the reclaim lock so it cannot race a concurrent reclaim's staging.
        Every outcome is logged and audited; returns the summary counts.
        """

        report = StagingSweepReport()
        roots = self._staging_roots()
        if not roots:
            return report
        with self._lock:
            records: List[ActionRecord] = []
            for root in roots:
                for sibling in self._find_staging_siblings(root):
                    self._reconcile_one_sibling(sibling, records, report)
            self._flush_audit(records)
        if report.total:
            LOGGER.info(
                "web reclaim: staging sweep restored=%s removed=%s would_remove=%s "
                "skipped=%s dry_run=%s",
                report.restored,
                report.removed,
                report.would_remove,
                report.skipped,
                self.dry_run,
            )
        return report

    def _staging_roots(self) -> Tuple[Path, ...]:
        """The mounted media roots to sweep: every configured ``container_prefix``,
        de-duplicated and de-nested (overlapping maps visit each subtree once) and
        filtered to those actually mounted as directories in this container."""

        prefixes = [container for _plex, container in self._config.web_media_path_map]
        return tuple(root for root in collapse_roots(prefixes) if root.is_dir())

    @staticmethod
    def _find_staging_siblings(root: Path) -> List[Path]:
        """Every ``*.uncc-reclaim`` file under ``root``, not following symlinked dirs
        (``followlinks=False``). ``os.walk`` swallows per-directory errors by default,
        so an unreadable subtree is skipped rather than aborting the sweep."""

        siblings: List[Path] = []
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                if len(name) > len(STAGING_SUFFIX) and name.endswith(STAGING_SUFFIX):
                    siblings.append(Path(dirpath) / name)
        return siblings

    def _reconcile_one_sibling(
        self, sibling: Path, records: List[ActionRecord], report: StagingSweepReport
    ) -> None:
        # A symlink bearing our suffix is not one of ours (staging always renames a
        # validated regular file) — never restore or delete through it.
        if os.path.islink(sibling):
            self._skip_sibling(sibling, records, report, "is a symlink, not a staging file")
            return
        original = self._original_for_staging(sibling)
        if original is None:
            self._skip_sibling(
                sibling,
                records,
                report,
                "name may be truncated to NAME_MAX; original path is not "
                "reconstructable — reconcile by hand",
            )
            return
        try:
            original_present = os.path.lexists(original)
        except OSError as exc:
            self._skip_sibling(sibling, records, report, f"could not stat {original}: {exc}")
            return
        if original_present:
            self._remove_leftover(sibling, original, records, report)
        else:
            self._restore_crash_staged(sibling, original, records, report)

    @staticmethod
    def _original_for_staging(sibling: Path) -> Optional[Path]:
        """The original media path a staging sibling maps back to — or ``None`` when
        the staged name may have been truncated to the directory's ``NAME_MAX``
        (:meth:`_staging_path`), so the original can't be reconstructed unambiguously
        and the sibling must be flagged instead of acted on."""

        base = sibling.name[: -len(STAGING_SUFFIX)]
        if not base:
            return None
        try:
            name_max = int(os.pathconf(sibling.parent, "PC_NAME_MAX"))
        except (OSError, ValueError, AttributeError):
            name_max = 255
        max_base = max(1, name_max - len(STAGING_SUFFIX.encode("utf-8")))
        # ``_staging_path`` only truncates when the original overflows ``max_base``,
        # cutting the base to exactly that budget. A staged base at (or over) the
        # budget is therefore indistinguishable from a truncation of a longer name —
        # so it is ambiguous. A shorter base was provably never truncated.
        if len(base.encode("utf-8", "surrogatepass")) >= max_base:
            return None
        return sibling.with_name(base)

    def _restore_crash_staged(
        self,
        sibling: Path,
        original: Path,
        records: List[ActionRecord],
        report: StagingSweepReport,
    ) -> None:
        # No-clobber by construction: only reached when the original is absent. Mirrors
        # the in-process rollback's lexists-then-rename under the lock; the sweep runs
        # before the server serves, so no concurrent writer can recreate the original
        # in the window.
        size = self._sibling_size(sibling)
        try:
            self._filesystem_mover(sibling, original)
        except OSError as exc:
            self._skip_sibling(
                sibling, records, report, f"could not restore to {original}: {exc}"
            )
            return
        report.restored += 1
        LOGGER.info("web reclaim: staging sweep restored %s -> %s", sibling, original)
        records.append(
            ActionRecord(
                path=original,
                action=RECONCILE_ACTION,
                status=STATUS_RESTORED,
                size=size,
                message=f"restored crash-staged file from {sibling}",
            )
        )

    def _remove_leftover(
        self,
        sibling: Path,
        original: Path,
        records: List[ActionRecord],
        report: StagingSweepReport,
    ) -> None:
        size = self._sibling_size(sibling)
        if self.dry_run:
            report.would_remove += 1
            LOGGER.info(
                "web reclaim: staging sweep would remove %s (dry-run; original %s present)",
                sibling,
                original,
            )
            records.append(
                ActionRecord(
                    path=original,
                    action=RECONCILE_ACTION,
                    status=STATUS_SKIPPED,
                    size=size,
                    message=f"dry-run: staging leftover {sibling} left in place (original present)",
                )
            )
            return
        try:
            self._filesystem_deleter(sibling)
        except OSError as exc:
            self._skip_sibling(
                sibling,
                records,
                report,
                f"could not remove leftover (original {original} present): {exc}",
            )
            return
        report.removed += 1
        LOGGER.info(
            "web reclaim: staging sweep removed leftover %s (original %s present)",
            sibling,
            original,
        )
        records.append(
            ActionRecord(
                path=original,
                action=RECONCILE_ACTION,
                status=STATUS_REMOVED,
                size=size,
                message=f"removed staging leftover {sibling}; original present",
            )
        )

    def _skip_sibling(
        self,
        sibling: Path,
        records: List[ActionRecord],
        report: StagingSweepReport,
        reason: str,
    ) -> None:
        report.skipped += 1
        LOGGER.warning("web reclaim: staging sweep skipped %s: %s", sibling, reason)
        records.append(
            ActionRecord(
                path=sibling,
                action=RECONCILE_ACTION,
                status=STATUS_SKIPPED,
                size=self._sibling_size(sibling),
                message=reason,
            )
        )

    @staticmethod
    def _sibling_size(sibling: Path) -> int:
        try:
            return os.lstat(sibling).st_size
        except OSError:
            return 0

    # -- per-target ---------------------------------------------------------- #

    def _reclaim_one(
        self,
        target: ReclaimTarget,
        index: _ActionIndex,
        seen_copies: Set[int],
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
        if any(part.plex_path in entry.keeper_paths for part in entry.parts):
            # A non-keeper copy that reuses one of the keeper's physical files (Plex
            # can report one file under two Media/Part ids): deleting it would
            # destroy the keeper's file, so refuse it even though it is not the
            # keeper by identity.
            return self._refused(
                target, "", "target shares a file with the keeper; never deleted"
            )
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
            return self._reclaim_arr(target, entry)
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

        # Two-phase move (#64): stage every part aside first, so a per-file failure
        # surfaces at the rename (where rollback is clean) instead of after an unlink
        # has irreversibly committed. Only once ALL parts stage does the unlink pass
        # run — so a stacked copy is truly removed whole or refused whole.
        staged, stage_error = self._stage_for_delete(target, validated)
        if stage_error is not None:
            return stage_error

        jobs = [
            _DeleteJob(
                # The audit and the operator-facing message name the *original* media
                # path, never the transient staging path.
                audit_path=original,
                size=size,
                perform=(lambda sp=staged_path: self._filesystem_deleter(sp)),
                deleted_message=f"rating_key={target.rating_key} part_id={target.part_id}",
                error_message=(
                    lambda exc: f"rating_key={target.rating_key} part_id={target.part_id}: {exc}"
                ),
                partial_message=(
                    lambda exc, done, p=original: (
                        f"partial: deleted {done} bytes, then failed on {p}: {exc}"
                    )
                ),
                staged_path=staged_path,
                leftover_message=(
                    lambda sp, p=original: (
                        f"rating_key={target.rating_key} part_id={target.part_id}: "
                        f"purge aborted; {p} left staged at {sp} "
                        "(reconciled on next startup)"
                    )
                ),
            )
            for original, staged_path, size in staged
        ]
        return self._execute_deletes(
            target,
            BACKEND_FILESYSTEM,
            jobs,
            action="web-reclaim:filesystem",
            catch=OSError,
            success_message=f"{len(staged)} file(s)",
            # If an unlink fails before ANY delete has committed, the copy is still
            # fully staged and recoverable: roll every part back and refuse with
            # nothing deleted, rather than leaving originals renamed away and the later
            # (un-attempted) staged parts orphaned and un-audited.
            on_uncommitted_failure=(
                lambda exc: self._refuse_and_rollback_purge(target, staged, exc)
            ),
        )

    def _refuse_and_rollback_purge(
        self, target: ReclaimTarget, staged: List[Tuple[Path, Path, int]], exc: BaseException
    ) -> ReclaimResult:
        """A staged part failed to unlink before any delete committed. Every part is
        still staged (the failed unlink raised, so its staged file also remains), so
        roll them all back and refuse with nothing deleted — preserving the
        whole-or-refused guarantee through the purge phase for the recoverable case."""

        orphans = self._rollback_staged(staged)
        return self._stage_failure_result(
            target, f"could not delete a staged part: {exc}", orphans
        )

    def _stage_for_delete(
        self, target: ReclaimTarget, validated: List[Tuple[Path, int]]
    ) -> Tuple[Optional[List[Tuple[Path, Path, int]]], Optional[ReclaimResult]]:
        """Phase 1 of the two-phase filesystem delete: rename every prevalidated part
        to its ``*.uncc-reclaim`` staging sibling, returning ``(staged, None)`` where
        ``staged`` is ``[(original, staged_path, size)]``.

        The rename is in-place (same directory → same filesystem), so it is atomic and
        cannot hit ``EXDEV`` even when parts resolve under different media roots. If any
        part fails to stage — a pre-existing staging sibling (an interrupted prior run,
        never clobbered) or an ``OSError`` — the parts already staged are rolled back
        and ``(None, error_result)`` is returned with **nothing deleted**. A rollback
        that itself fails leaves that part orphaned at its staging path; those are the
        only records audited here (a clean rollback mutates nothing, so it audits
        nothing — mirroring a validation refusal)."""

        staged: List[Tuple[Path, Path, int]] = []
        for original, size in validated:
            staged_path = self._staging_path(original)
            reason: Optional[str] = None
            if os.path.lexists(staged_path):
                reason = (
                    f"a stale staging file {staged_path.name} already exists next to "
                    f"{original} (an earlier reclaim was interrupted?); refusing to "
                    "clobber it — remove it by hand and retry"
                )
            else:
                try:
                    self._filesystem_mover(original, staged_path)
                except OSError as exc:
                    reason = f"could not stage {original} for deletion: {exc}"
            if reason is not None:
                orphans = self._rollback_staged(staged)
                return None, self._stage_failure_result(target, reason, orphans)
            staged.append((original, staged_path, size))
        return staged, None

    @staticmethod
    def _staging_path(original: Path) -> Path:
        """The staging sibling for ``original`` — its basename plus ``STAGING_SUFFIX``,
        but bounded so the component never exceeds the directory's ``NAME_MAX``.

        A media file whose basename is already near the limit (e.g. 244+ bytes on
        ext4's 255-byte cap) would otherwise make ``basename + '.uncc-reclaim'`` too
        long, failing the stage rename with ``ENAMETOOLONG`` and refusing a reclaim a
        direct unlink would have handled. The base is truncated on a byte boundary (the
        limit is bytes, not characters); a truncation collision with another sibling is
        caught by the ``lexists`` guard in :meth:`_stage_for_delete` and refused, never
        silently clobbered."""

        suffix = STAGING_SUFFIX
        try:
            name_max = int(os.pathconf(original.parent, "PC_NAME_MAX"))
        except (OSError, ValueError, AttributeError):
            name_max = 255  # conservative default (ext4/xfs/btrfs/zfs)
        encoded = original.name.encode("utf-8", "surrogatepass")
        max_base = max(1, name_max - len(suffix.encode("utf-8")))
        if len(encoded) > max_base:
            base = encoded[:max_base].decode("utf-8", "ignore")
        else:
            base = original.name
        return original.with_name(base + suffix)

    def _rollback_staged(
        self, staged: List[Tuple[Path, Path, int]]
    ) -> List[Tuple[Path, Path, int]]:
        """Best-effort restore of already-staged parts (reverse order), returning the
        parts that could **not** be restored — orphaned at their staging path. A
        rollback failure is the only path in the two-phase delete that leaves the
        filesystem changed, so the caller audits those orphans."""

        orphans: List[Tuple[Path, Path, int]] = []
        for original, staged_path, size in reversed(staged):
            if os.path.lexists(original):
                # The original path reappeared during the staging window (another
                # process recreated the file). ``os.rename`` would silently clobber it,
                # destroying a new file while we report "nothing deleted" — so leave the
                # staged copy as an orphan for manual reconciliation instead.
                LOGGER.error(
                    "reclaim rollback skipped: %s reappeared; leaving staged copy at %s",
                    original,
                    staged_path,
                )
                orphans.append((original, staged_path, size))
                continue
            try:
                self._filesystem_mover(staged_path, original)
            except OSError as exc:
                LOGGER.error(
                    "reclaim rollback failed: %s could not be restored to %s (%s)",
                    staged_path,
                    original,
                    exc,
                )
                orphans.append((original, staged_path, size))
        return orphans

    def _stage_failure_result(
        self,
        target: ReclaimTarget,
        reason: str,
        orphans: List[Tuple[Path, Path, int]],
    ) -> ReclaimResult:
        """Build the ``error`` result for a staging failure (0 bytes reclaimed),
        auditing any parts a failed rollback left orphaned at their staging path."""

        if orphans:
            records = [
                ActionRecord(
                    path=original,
                    action="web-reclaim:filesystem",
                    status=STATUS_ERROR,
                    size=size,
                    message=(
                        f"rating_key={target.rating_key} part_id={target.part_id}: "
                        f"could not restore original; file left staged at {staged_path}"
                    ),
                )
                for original, staged_path, size in orphans
            ]
            self._flush_audit(records)
            detail = (
                f"; {len(orphans)} file(s) could not be rolled back and remain staged "
                "(manual restore needed)"
            )
        else:
            detail = "; rolled back, nothing deleted"
        return self._error(target, BACKEND_FILESYSTEM, 0, f"{reason}{detail}")

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

    def _reclaim_arr(self, target: ReclaimTarget, entry: _CopyEntry) -> ReclaimResult:
        backend = entry.arr_tracked or ""
        client = self._arr_client(backend)
        if client is None:
            return self._refused(
                target, backend, f"{backend or 'arr'} client is not configured for actions"
            )

        # Resolve every part to a live, re-validated *arr file id BEFORE any
        # DELETE, so a stacked copy is removed whole or refused whole. The id is
        # read from the report (serialized at report time, #61); a single by-id GET
        # re-validates it — a 404 (already removed) or a current-path basename that
        # differs from the report's (the id was reused for another file) refuses the
        # whole copy. No full-library fan-out: at most one GET + one DELETE per part.
        resolved, error = self._resolve_arr_targets(backend, client, entry)
        if error is not None or resolved is None:
            return self._refused(target, backend, error or "arr resolution failed")

        total = sum(size for _, _, size in resolved)
        if self.dry_run:
            return self._would_delete(target, backend, total, f"{len(resolved)} file(s) via {backend}")

        jobs = [
            _DeleteJob(
                audit_path=plex_path,
                size=size,
                perform=(lambda fid=file_id: self._delete_arr(client, backend, fid)),
                deleted_message=(
                    f"id={file_id} rating_key={target.rating_key} part_id={target.part_id}"
                ),
                error_message=(
                    lambda exc, fid=file_id: (
                        f"id={fid} rating_key={target.rating_key} "
                        f"part_id={target.part_id}: {exc}"
                    )
                ),
                partial_message=(
                    lambda exc, done, name=plex_path.name: (
                        f"partial: {backend} delete failed for {name}: {exc}"
                    )
                ),
            )
            for file_id, plex_path, size in resolved
        ]
        return self._execute_deletes(
            target,
            backend,
            jobs,
            action=f"web-reclaim:{backend}",
            catch=ArrClientError,
            success_message=f"{len(resolved)} file(s) via {backend}",
        )

    def _resolve_arr_targets(
        self, backend: str, client: object, entry: _CopyEntry
    ) -> Tuple[Optional[List[Tuple[int, Path, int]]], Optional[str]]:
        """Re-validate every part's report-serialized ``*arr`` file id by a single
        by-id GET, returning ``([(file_id, plex_path, size)], None)`` or
        ``(None, reason)``.

        Fail-closed on every axis: a part with no serialized id (a report predating
        #61, or an id that could not be pinned unambiguously) is refused; a 404 means
        the id is gone; and the by-id record is re-anchored to the report on *both*
        the current basename *and* the current size — the same two-signal drift guard
        the filesystem backend applies (basename bridges mount-path differences, size
        distinguishes a same-named-but-different file the id may have been reused for).
        Any single failure refuses the whole (possibly stacked) copy, so a multi-part
        copy is deleted whole or refused whole — never after one DELETE has committed.
        """

        resolved: List[Tuple[int, Path, int]] = []
        for part in entry.parts:
            file_id = part.arr_file_id
            if file_id is None:
                return None, (
                    f"no unambiguous {backend} file id is recorded for "
                    f"{part.plex_path.name}; regenerate the duplicate report, or (if it "
                    f"is tracked in more than one place) remove it via {backend} directly"
                )
            try:
                current = self._get_arr_file(client, backend, file_id)
            except ArrClientError as exc:
                if getattr(exc, "status_code", None) == 404:
                    return None, (
                        f"{backend} file id {file_id} no longer exists "
                        f"({part.plex_path.name} already removed?)"
                    )
                return None, f"could not re-validate {backend} file id {file_id}: {exc}"
            current_path = current.get("path") or current.get("relativePath") or ""
            current_name = Path(str(current_path)).name
            if not current_name or current_name != part.plex_path.name:
                return None, (
                    f"{backend} file id {file_id} now points at "
                    f"{current_name or 'an unnamed file'}, not {part.plex_path.name} "
                    "(library drift); refusing"
                )
            # Size is the discriminator basename can't provide: an id reused for a
            # different file that happens to share the basename (generic episode
            # names collide across series) is caught here. Only enforced when the
            # *arr reports a positive size and the report recorded one, so a missing
            # size never falsely refuses a correct same-file reclaim.
            current_size = _as_opt_int(current.get("size"))
            if current_size is not None and part.size > 0 and current_size != part.size:
                return None, (
                    f"{backend} file id {file_id} size changed since the report "
                    f"({current_size} != {part.size}); refusing a stale/reused id"
                )
            resolved.append((file_id, part.plex_path, part.size))
        return resolved, None

    def _arr_client(self, backend: str) -> Optional[object]:
        if backend == arr.RADARR:
            return self._radarr
        if backend == arr.SONARR:
            return self._sonarr
        return None

    @staticmethod
    def _get_arr_file(client: object, backend: str, file_id: int) -> dict:
        """One by-id GET of the current ``*arr`` file record (fail-closed: may raise
        ``ArrClientError``, carrying ``status_code=404`` when the id is gone)."""

        if backend == arr.RADARR:
            return client.get_movie_file(file_id)  # type: ignore[attr-defined]
        return client.get_episode_file(file_id)  # type: ignore[attr-defined]

    @staticmethod
    def _delete_arr(client: object, backend: str, file_id: int) -> None:
        if backend == arr.RADARR:
            client.delete_movie_file(file_id)  # type: ignore[attr-defined]
        else:
            client.delete_episode_file(file_id)  # type: ignore[attr-defined]

    # -- shared delete-and-audit loop ---------------------------------------- #

    def _execute_deletes(
        self,
        target: ReclaimTarget,
        backend: str,
        jobs: Sequence[_DeleteJob],
        *,
        action: str,
        catch: type[BaseException],
        success_message: str,
        on_uncommitted_failure: Optional[Callable[[BaseException], ReclaimResult]] = None,
    ) -> ReclaimResult:
        """Run the prevalidated per-part delete-and-audit loop shared by both
        backends — the one place that owns the security-sensitive partial-failure
        protocol (whole-or-refused audit, deleted-byte accounting, flush timing).

        For each job: call its deleter; on the first failure (an exception of type
        ``catch``) append an ``error`` :class:`~.models.ActionRecord`, flush the audit
        batch, and return a ``partial`` error :class:`ReclaimResult`; otherwise add the
        freed bytes, append a ``deleted`` record, and continue. On completion, flush
        the batch and return a ``deleted`` result. Callers own prevalidation — it runs
        fully before any job here — and supply the deleter, the exception it raises,
        and every per-part/summary message (see :class:`_DeleteJob`).

        ``on_uncommitted_failure`` (filesystem only) is invoked instead of the
        partial-error path when a deleter fails *before any delete has committed*
        (``deleted_bytes == 0``): the backend can then undo the whole operation
        cleanly and produce its own result, so no partial-delete audit row is written
        for an outcome that is about to be rolled back. A failure after the first
        commit is irreversible, so it always takes the partial-error path.
        """

        records: List[ActionRecord] = []
        deleted_bytes = 0
        for index, job in enumerate(jobs):
            try:
                job.perform()
            except catch as exc:  # the backend deleter's own typed failure
                if deleted_bytes == 0 and on_uncommitted_failure is not None:
                    return on_uncommitted_failure(exc)
                records.append(
                    ActionRecord(
                        path=job.audit_path,
                        action=action,
                        status=STATUS_ERROR,
                        size=job.size,
                        message=job.error_message(exc),
                    )
                )
                # Post-commit purge failure (filesystem, #72): a prior unlink already
                # committed, so this is irreversible. The failed part AND every
                # un-attempted part behind it remain at their staging siblings; audit
                # each as a "left staged" row so no leftover is silent and the startup
                # sweep's later reconciliation is attributable. ``*arr`` jobs carry no
                # ``staged_path``, so this loop is a no-op for them.
                records.extend(self._staged_leftover_records(jobs[index:], action))
                self._flush_audit(records)
                return self._error(
                    target, backend, deleted_bytes, job.partial_message(exc, deleted_bytes)
                )
            deleted_bytes += job.size
            records.append(
                ActionRecord(
                    path=job.audit_path,
                    action=action,
                    status=STATUS_DELETED,
                    size=job.size,
                    message=job.deleted_message,
                )
            )
        self._flush_audit(records)
        return self._deleted(target, backend, deleted_bytes, success_message)

    @staticmethod
    def _staged_leftover_records(
        remaining: Sequence[_DeleteJob], action: str
    ) -> List[ActionRecord]:
        """"Left staged" audit rows for the still-staged parts of an aborted purge —
        the part that failed to unlink plus every un-attempted part behind it. Only
        jobs carrying a ``staged_path`` (filesystem) contribute a row; the startup
        sweep reconciles each named sibling on the next run (#72)."""

        return [
            ActionRecord(
                path=job.audit_path,
                action=action,
                status=STATUS_ERROR,
                size=job.size,
                message=job.leftover_message(job.staged_path),
            )
            for job in remaining
            if job.staged_path is not None and job.leftover_message is not None
        ]

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
