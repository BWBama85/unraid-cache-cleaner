"""Core data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
    """One physical file backing a Plex media item."""

    part_id: int
    file: Path
    size: int
    resolution: str = ""
    bitrate: int = 0
    codec: str = ""
    container: str = ""


@dataclass(frozen=True)
class DuplicateGroup:
    """A Plex item that resolves to more than one media copy on disk."""

    rating_key: str
    kind: str
    title: str
    copies: tuple[MediaCopy, ...]
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    external_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class DuplicateReport:
    """Mutable result from a Plex duplicate scan, serialized like RunReport."""

    generated_at: float
    sections: tuple[PlexSection, ...] = ()
    groups: list[DuplicateGroup] = field(default_factory=list)
    total_groups: int = 0
    total_copies: int = 0
    reclaimable_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
