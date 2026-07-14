"""Optional content-hash confirmation pass for the Plex duplicate report (#9).

Plex groups duplicates by metadata and size, not content. This opt-in pass
(``HASH_MODE`` != ``off``) reads the actual bytes of an ``identical`` group's
copies and either:

* **confirms** they are byte-for-byte identical (``full`` mode) or that their
  sampled regions agree (``partial`` mode) — reclaim stays as computed, the report
  labels the group hash-confirmed / sample-matched; or
* **downgrades** a group whose copies prove *different* to ``different-content``
  (:data:`dedupe.DIFFERENT`) with ``reclaimable_bytes = 0`` — one copy is not a
  redundant duplicate of the other, so it must never be reclaimed; or
* **flags** a group ``unhashable`` when a copy cannot be located/read — it stays
  size-only reclaimable (unchanged) but is never called confirmed, because an
  unverified copy is never silently treated as safe.

The pass is read-only and fail-closed on every axis: it opens **only** Plex-reported
paths translated through the shared, component-aware path map (:func:`planner.map_media_path`,
the same one the web action layer's filesystem delete uses), refuses symlinks /
non-regular files / paths that escape their mapped root, and re-verifies each file's
on-disk size against the size Plex reported before hashing a single byte. It never
walks the filesystem — an unmapped or unmounted copy is simply unhashable.

``partial`` reads size + the first and last 4 MiB of each part via ``seek``, a strong
identity signal at constant cost regardless of file size (a 60 GB remux is not read
end-to-end unless ``HASH_MODE=full``). Because the middle is unread, a ``partial``
match is reported as ``sample-match`` — a strong signal, **never** proof — and only
``full`` yields the byte-for-byte ``confirmed`` verdict. A ``partial`` *mismatch* is
still decisive: differing sampled bytes prove the copies differ.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat as stat_mod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import dedupe
from .models import DuplicateGroup, MediaCopy
from .planner import map_media_path

LOGGER = logging.getLogger(__name__)

# HASH_MODE values (mirror config.HASH_MODES).
HASH_OFF = "off"
HASH_PARTIAL = "partial"
HASH_FULL = "full"

# DuplicateGroup.hash_status values set by this pass.
CONFIRMED = "confirmed"
SAMPLE_MATCH = "sample-match"
UNHASHABLE = "unhashable"
DIFFERENT = "different"

#: Head/tail sample size for ``partial`` mode. A part at or below twice this is read
#: whole (head and tail would otherwise overlap), so ``partial`` is never more work
#: than ``full`` for a small file and strictly less for a large one.
_SAMPLE = 4 * 1024 * 1024
#: Streaming block for whole-file reads, so ``full`` mode never loads a large file
#: into memory at once.
_READ_BLOCK = 1024 * 1024


def hash_regions(size: int, mode: str) -> Tuple[Tuple[int, int], ...]:
    """Return the ``(offset, length)`` byte regions hashing a ``size``-byte part reads.

    Pure and side-effect-free so the "``partial`` never reads the whole file"
    guarantee is unit-testable without touching the filesystem: ``full`` returns the
    whole extent, ``partial`` returns just the head and tail samples (or the whole
    file when it is small enough that they would overlap). A zero-byte part reads
    nothing in either mode.
    """

    if size <= 0:
        return ()
    if mode == HASH_FULL or size <= 2 * _SAMPLE:
        return ((0, size),)
    return ((0, _SAMPLE), (size - _SAMPLE, _SAMPLE))


@dataclass(frozen=True)
class CopyHash:
    """Per-logical-copy hash outcome.

    ``topology`` is the ordered tuple of the copy's physical part sizes — two copies
    are only comparable when their topologies match (same split), so a stacked copy
    is never falsely called different-content merely because it is split differently.
    ``digest`` is ``None`` when the copy could not be hashed, in which case ``error``
    explains why (surfaced as a report warning).
    """

    topology: Tuple[int, ...]
    digest: Optional[str]
    error: Optional[str]


def _resolve_part(
    plex_path: Path, path_map: Sequence[Tuple[Path, Path]], expected_size: int
) -> Tuple[Optional[Path], Optional[str]]:
    """Translate + safety-check one Plex part path, mirroring the reclaim path.

    Returns ``(container_path, None)`` when the file is a real, in-root regular file
    whose on-disk size matches what Plex reported, else ``(None, reason)``. Fail-closed
    on every branch: unmapped path, missing/unreadable file, symlink or non-regular
    file, a realpath that escapes the mapped root, or a size that drifted since the
    report.
    """

    mapped = map_media_path(plex_path, path_map)
    if mapped is None:
        return None, f"path not mapped by WEB_MEDIA_PATH_MAP: {plex_path}"
    container_path, container_prefix = mapped

    try:
        info = os.lstat(container_path)
    except OSError as exc:
        return None, f"not readable: {container_path} ({exc.__class__.__name__})"
    if not stat_mod.S_ISREG(info.st_mode):
        return None, f"not a regular file (symlink or directory?): {container_path}"

    real_path = Path(os.path.realpath(container_path))
    real_root = Path(os.path.realpath(container_prefix))
    try:
        real_path.relative_to(real_root)
    except ValueError:
        return None, f"resolved path escapes the media root: {container_path}"

    if info.st_size != expected_size:
        return None, (
            f"size changed since the report ({info.st_size} on disk != {expected_size} "
            f"reported): {container_path}"
        )
    return container_path, None


def _hash_copy(
    parts: Sequence[MediaCopy], path_map: Sequence[Tuple[Path, Path]], mode: str
) -> CopyHash:
    """Hash one logical copy: its ordered parts fed into a single digest.

    The digest is over content bytes only (no framing), so a single-part ``full``
    digest equals a plain ``sha256`` of the file — a clean, testable property. Any
    part that fails :func:`_resolve_part` makes the whole copy unhashable.
    """

    topology = tuple(part.size for part in parts)
    digest = hashlib.sha256()
    for part in parts:
        container_path, error = _resolve_part(part.file, path_map, part.size)
        if error is not None or container_path is None:
            return CopyHash(topology=topology, digest=None, error=error)
        try:
            with open(container_path, "rb") as handle:
                for offset, length in hash_regions(part.size, mode):
                    handle.seek(offset)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(_READ_BLOCK, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
        except OSError as exc:
            return CopyHash(
                topology=topology,
                digest=None,
                error=f"read failed: {container_path} ({exc.__class__.__name__})",
            )
    return CopyHash(topology=topology, digest=digest.hexdigest(), error=None)


def _roots_mounted(path_map: Sequence[Tuple[Path, Path]]) -> bool:
    """True when at least one mapped container root exists as a directory.

    Distinguishes "the media volume is not mounted here" (skip the pass, warn once)
    from a single missing file (that copy is unhashable, others proceed)."""

    return any(
        container_prefix.exists() and container_prefix.is_dir()
        for _plex_prefix, container_prefix in path_map
    )


def _confirm_status(mode: str) -> str:
    """A digest *match* means ``confirmed`` under ``full`` (proof) but only
    ``sample-match`` under ``partial`` (the middle was never read)."""

    return CONFIRMED if mode == HASH_FULL else SAMPLE_MATCH


def _confirm_one(
    group: DuplicateGroup, path_map: Sequence[Tuple[Path, Path]], mode: str
) -> Tuple[DuplicateGroup, Optional[str]]:
    """Hash one ``identical`` group and return it re-tagged plus an optional warning."""

    pairs = dedupe.rank_copies_with_parts(group)
    hashes = [_hash_copy(parts, path_map, mode) for _logical, parts in pairs]

    unreadable = [h.error for h in hashes if h.digest is None]
    if unreadable:
        warning = (
            f"Content hash: '{group.title}' left size-only (unhashable): "
            f"{unreadable[0]}" + (f" (+{len(unreadable) - 1} more)" if len(unreadable) > 1 else "")
        )
        return replace(group, hash_status=UNHASHABLE), warning

    topologies = {h.topology for h in hashes}
    if len(topologies) > 1:
        # Copies split into different part layouts cannot be compared byte-for-byte
        # without guessing; leave the group size-only rather than risk a false
        # different-content downgrade of same content stored differently.
        warning = (
            f"Content hash: '{group.title}' left size-only (copies have different "
            "part layouts; not comparable)"
        )
        return replace(group, hash_status=UNHASHABLE), warning

    digests = {h.digest for h in hashes}
    if len(digests) == 1:
        return replace(group, hash_status=_confirm_status(mode)), None

    # Digests differ: Plex grouped same-size copies that are not the same bytes. One
    # is not a redundant duplicate, so protect the whole group from reclaim.
    warning = (
        f"Content hash: '{group.title}' downgraded to different-content "
        "(same size, different bytes); excluded from reclaimable"
    )
    downgraded = replace(
        group,
        classification=dedupe.DIFFERENT,
        hash_status=DIFFERENT,
        reclaimable_bytes=0,
        reclaimable_keep_smallest=0,
    )
    return downgraded, warning


def confirm_groups(
    groups: Sequence[DuplicateGroup],
    path_map: Sequence[Tuple[Path, Path]],
    mode: str,
) -> Tuple[List[DuplicateGroup], List[str]]:
    """Run the content-hash pass over analyzed groups.

    Only ``identical`` groups are examined (an ``upgrade``'s copies are meant to
    differ, and a ``mismatch`` is already protected). Every other group is returned
    unchanged. Returns the re-tagged groups in the same order plus any warnings.

    Fail-closed when media is not reachable: with no path map, or with a map whose
    roots are all unmounted, the pass is skipped whole (one warning) and the report
    is served from Plex data alone — never crashing a read-only report over a missing
    mount.
    """

    if mode == HASH_OFF:
        return list(groups), []
    if not path_map:
        return list(groups), [
            f"HASH_MODE={mode} but WEB_MEDIA_PATH_MAP is not set; cannot locate media — "
            "skipping the content-hash pass (report is size-only)."
        ]
    if not _roots_mounted(path_map):
        return list(groups), [
            f"HASH_MODE={mode} but no WEB_MEDIA_PATH_MAP container root is mounted here; "
            "skipping the content-hash pass (report is size-only)."
        ]

    result: List[DuplicateGroup] = []
    warnings: List[str] = []
    for group in groups:
        if group.classification != dedupe.IDENTICAL:
            result.append(group)
            continue
        confirmed, warning = _confirm_one(group, path_map, mode)
        result.append(confirmed)
        if warning is not None:
            warnings.append(warning)
    return result, warnings
