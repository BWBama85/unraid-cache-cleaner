"""Core data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Extraction-claim outcomes — the shared vocabulary returned by the SQLite ledger
# (`state.py`) and interpreted by the extractor. Kept here, the neutral data home,
# so the persistence layer need not import the feature module.
CLAIM_NEW = "new"  # caller won the claim; proceed to extract
CLAIM_DONE = "done"  # already extracted; skip with no re-invoke
CLAIM_BUSY = "busy"  # a fresh claim is held elsewhere; skip this cycle


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of claiming an archive for extraction (#41).

    ``decision`` is one of the ``CLAIM_*`` constants. ``token`` is a random
    per-claim ownership token, set only when ``decision == CLAIM_NEW``: the caller
    must present it back to ``complete``/``release`` so a claim reclaimed by a
    concurrent run after the TTL cannot be silently promoted or dropped by the
    original (crashed-then-resurrected) owner. It is ``None`` for ``CLAIM_DONE`` /
    ``CLAIM_BUSY``, where the caller neither extracts nor records.
    """

    decision: str
    token: str | None = None


@dataclass(frozen=True)
class TorrentRecord:
    """Minimal torrent data needed for cleanup decisions."""

    torrent_hash: str
    name: str
    state: str
    save_path: Path
    content_path: Path
    progress: float = 0.0


@dataclass(frozen=True)
class FileRecord:
    """Filesystem file metadata."""

    path: Path
    size: int
    mtime: float


@dataclass(frozen=True)
class CandidateRecord(FileRecord):
    """A current orphan candidate with state timestamps."""

    first_seen: float
    last_seen: float


@dataclass(frozen=True)
class ActionRecord:
    """Deletion or directory-removal outcome."""

    path: Path
    action: str
    status: str
    size: int = 0
    message: str = ""


@dataclass(frozen=True)
class ProtectionPlan:
    """Paths that should never be treated as orphan content."""

    tracked_files: frozenset[Path]
    protected_dirs: tuple[Path, ...]


@dataclass
class RunReport:
    """High-level result from a scan cycle."""

    started_at: float
    finished_at: float
    dry_run: bool
    watch_roots: tuple[Path, ...]
    torrent_count: int
    protected_dir_count: int
    tracked_file_count: int
    scanned_file_count: int
    orphan_candidate_count: int
    eligible_count: int
    actions: list[ActionRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlexSection:
    """A Plex library section (Movies, TV Shows, ...)."""

    key: str
    type: str
    title: str


@dataclass(frozen=True)
class MediaCopy:
    """One physical file backing a Plex media item.

    ``media_id`` groups the parts of a single Plex ``Media`` element (a stacked
    item split across several files). Copies sharing a non-zero ``media_id`` are
    one logical copy — the dedupe engine merges them and sums their sizes rather
    than counting them as duplicates. ``0`` means "ungrouped": each such copy
    stands alone.

    ``association`` / ``arr_tracked`` are populated by the optional Radarr/Sonarr
    layer (#8): ``association`` is ``"tracked"`` (an ``*arr`` tracks this file, so
    deleting it triggers a re-download), ``"untracked"`` (safe to delete), or
    ``"unknown"`` (could not be confirmed — never treat as safe). ``arr_tracked``
    names the tracking service (``"radarr"`` / ``"sonarr"``) when tracked, else
    ``None``. All stay at their defaults on a Plex-only run.

    ``arr_file_id`` is the ``*arr`` ``movieFile``/``episodeFile`` id backing *this
    physical file*, captured during annotation so the web action layer can delete a
    tracked copy by id — an ``O(1)``, drift-safe reclaim — instead of resolving the
    id live by basename at delete time (#61). It is per-*part* (never propagated
    across a stack, since each part is a distinct ``*arr`` file), and stays ``None``
    when the copy is not tracked or the id could not be pinned unambiguously.
    """

    part_id: int
    file: Path
    size: int
    resolution: str = ""
    bitrate: int = 0
    codec: str = ""
    container: str = ""
    media_id: int = 0
    association: str = "unknown"
    arr_tracked: str | None = None
    arr_file_id: int | None = None


@dataclass(frozen=True)
class DuplicateGroup:
    """A Plex item that resolves to more than one media copy on disk.

    The trailing analysis fields (``keeper`` … ``reclaimable_keep_smallest``) are
    populated by ``dedupe.analyze``; a freshly parsed group leaves them at their
    defaults.
    """

    rating_key: str
    kind: str
    title: str
    copies: tuple[MediaCopy, ...]
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    keeper: MediaCopy | None = None
    classification: str = ""
    reclaimable_bytes: int = 0
    reclaimable_keep_smallest: int = 0
    # Outcome of the optional content-hash confirmation pass (#9), populated only
    # when ``HASH_MODE`` != ``off`` and the group was an ``identical`` candidate.
    # ``""`` = pass did not run / not applicable; ``"confirmed"`` = every copy is
    # byte-for-byte identical (``full`` mode only); ``"sample-match"`` = the sampled
    # regions (size + first/last 4 MiB) agree but the middle was not read
    # (``partial`` mode — a strong signal, never proof); ``"unhashable"`` = at least
    # one copy could not be read/mapped, so the group stays size-only and is never
    # upgraded to confirmed. A group whose copies proved *different* is reclassified
    # ``different-content`` (see :mod:`dedupe`) and carries ``hash_status="different"``.
    hash_status: str = ""


@dataclass(frozen=True)
class SectionSummary:
    """Per-section (by ``DuplicateGroup.kind``) duplicate totals."""

    kind: str
    group_count: int
    copy_count: int
    identical_count: int
    upgrade_count: int
    mismatch_count: int
    reclaimable_bytes: int
    reclaimable_keep_smallest: int
    # Content-hash pass tallies (#9), all ``0`` unless ``HASH_MODE`` != ``off``:
    # groups reclassified different-content (excluded from reclaimable like a
    # mismatch), and — among the ``identical`` groups — how many were byte-for-byte
    # confirmed, sample-matched (partial), or left size-only because a copy was
    # unhashable. Appended with defaults so existing ``SectionSummary(...)`` calls
    # keep working.
    different_count: int = 0
    hash_confirmed_count: int = 0
    hash_sample_match_count: int = 0
    hash_unhashable_count: int = 0


@dataclass(frozen=True)
class DedupeSummary:
    """Overall duplicate totals plus a per-section breakdown.

    Reclaimable figures exclude ``mismatch`` groups by construction — the hard
    safety rule is that a group Plex merged from different titles is never
    counted as reclaimable. The optional content-hash pass (#9) extends that rule
    to ``different-content`` groups (copies proven to differ) and never inflates
    reclaimable from an unconfirmed size-only group.
    """

    sections: tuple[SectionSummary, ...]
    group_count: int
    copy_count: int
    identical_count: int
    upgrade_count: int
    mismatch_count: int
    reclaimable_bytes: int
    reclaimable_keep_smallest: int
    # Aggregate content-hash tallies mirroring the per-section fields above (#9);
    # appended with defaults so existing ``DedupeSummary(...)`` calls keep working.
    different_count: int = 0
    hash_confirmed_count: int = 0
    hash_sample_match_count: int = 0
    hash_unhashable_count: int = 0


@dataclass
class DuplicateReport:
    """Mutable result from a Plex duplicate scan, serialized like RunReport."""

    generated_at: float
    sections: tuple[PlexSection, ...] = ()
    groups: list[DuplicateGroup] = field(default_factory=list)
    total_groups: int = 0
    total_copies: int = 0
    reclaimable_bytes: int = 0
    summary: DedupeSummary | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # True when the Radarr/Sonarr association layer (#8) ran. Gates whether the
    # per-copy association fields are serialized, so a Plex-only run omits them.
    arr_enabled: bool = False
    # True when the content-hash confirmation pass (#9) ran (``HASH_MODE`` != off).
    # Gates whether the per-group ``hash_status`` and the hash totals are
    # serialized, so an ``off`` run keeps the existing report shape byte-for-byte.
    hash_enabled: bool = False
