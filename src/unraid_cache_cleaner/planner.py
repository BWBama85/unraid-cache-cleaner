"""Protection planning and orphan candidate identification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, Union

from .models import FileRecord, ProtectionPlan, TorrentRecord


def normalize_path(path: Union[Path, str]) -> Path:
    """Normalize a path without dereferencing symlinks."""

    return Path(os.path.abspath(os.path.normpath(str(path))))


def map_media_path(
    plex_path: Path, path_map: Sequence[Tuple[Path, Path]]
) -> Optional[Tuple[Path, Path]]:
    """Map a Plex-reported path to ``(container_path, container_prefix)`` via ``path_map``.

    Longest-prefix and **component-aware**, so ``/mnt/user/Media`` never matches
    ``/mnt/user/Media2``; a ``..`` in the remainder is refused (never joined) so a
    crafted Plex path cannot escape its mapped root. Returns ``None`` when no prefix
    matches — the caller must treat an unmapped path as fail-closed (a filesystem
    delete is refused, a hash is marked unhashable), never guess.

    Shared by the web action layer's filesystem reclaim (#34) and the content-hash
    confirmation pass (#9): both translate the same Plex ``Part.file`` to this
    container's mounts, so the mapping lives here rather than being duplicated.
    """

    plex_parts = plex_path.parts
    best: Optional[Tuple[int, Path, Path]] = None
    for plex_prefix, container_prefix in path_map:
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


def is_within(path: Path, root: Path) -> bool:
    """Return True when path is equal to or inside root."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    """Return True when path falls under any configured root."""

    return any(is_within(path, root) for root in roots)


def collapse_roots(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Remove duplicate and nested roots."""

    unique_paths = sorted({normalize_path(path) for path in paths}, key=lambda item: len(item.parts))
    collapsed: list[Path] = []
    for path in unique_paths:
        if any(is_within(path, existing) for existing in collapsed):
            continue
        collapsed.append(path)
    return tuple(collapsed)


def _treat_content_as_directory(content_path: Path) -> bool:
    if content_path.exists():
        return content_path.is_dir()
    return False


def build_protection_plan(
    torrents: list[TorrentRecord],
    watch_roots: tuple[Path, ...],
    *,
    protect_single_file_parent_dirs: bool,
) -> ProtectionPlan:
    """Build exact file and directory protections from active torrents."""

    tracked_files: set[Path] = set()
    protected_dirs: set[Path] = set()
    normalized_roots = collapse_roots(watch_roots)

    for torrent in torrents:
        content_path = normalize_path(torrent.content_path)
        save_path = normalize_path(torrent.save_path)

        if not is_within_any(content_path, normalized_roots) and not is_within_any(save_path, normalized_roots):
            continue

        if _treat_content_as_directory(content_path):
            protected_dirs.add(content_path)
            continue

        tracked_files.add(content_path)

        if protect_single_file_parent_dirs:
            parent = content_path.parent
            if parent not in normalized_roots and is_within_any(parent, normalized_roots):
                protected_dirs.add(parent)

    ordered_dirs = tuple(sorted({normalize_path(path) for path in protected_dirs}, key=lambda item: len(item.parts), reverse=True))
    return ProtectionPlan(
        tracked_files=frozenset(normalize_path(path) for path in tracked_files),
        protected_dirs=ordered_dirs,
    )


def with_protected_files(plan: ProtectionPlan, extra_files: Iterable[Path]) -> ProtectionPlan:
    """Return a plan with ``extra_files`` added as first-party tracked files.

    Extracted output is protected as **exact file paths** rather than by
    protecting its directory: an archive can sit directly at a watch root, and
    protecting that whole directory would disable all cleanup. Exact-path
    protection keeps the extracted media safe while leaving unrelated siblings —
    and the mount itself — eligible for orphan cleanup.
    """

    files = set(plan.tracked_files)
    files.update(normalize_path(path) for path in extra_files)
    return ProtectionPlan(
        tracked_files=frozenset(files),
        protected_dirs=plan.protected_dirs,
    )


def find_orphan_candidates(
    scanned_files: list[FileRecord],
    protection_plan: ProtectionPlan,
) -> dict[Path, FileRecord]:
    """Return current orphan candidates."""

    orphaned: dict[Path, FileRecord] = {}
    for record in scanned_files:
        normalized = normalize_path(record.path)
        if normalized in protection_plan.tracked_files:
            continue
        if is_within_any(normalized, protection_plan.protected_dirs):
            continue
        orphaned[normalized] = FileRecord(path=normalized, size=record.size, mtime=record.mtime)
    return orphaned
