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

The pass additionally **annotates** ``upgrade`` groups (#93). An upgrade has no
group-wide verdict to reach — its copies are supposed to differ — but it can still hold
two copies of the exact same size that are byte-identical. Those same-size buckets are
hashed and recorded as :class:`~models.HashBucket` verdicts on the group
(:func:`_confirm_upgrade`); a size unique within the group is never read. This is
**reporting only**: unlike the ``identical`` path above, it never reclassifies, never
protects, and never moves a keeper or a reclaimable figure. See
:func:`_confirm_upgrade` for why that asymmetry is the safe reading.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat as stat_mod
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from . import dedupe
from .models import DuplicateGroup, HashBucket, MediaCopy
from .planner import map_media_path

if TYPE_CHECKING:  # only for the type annotation; no runtime coupling to state.py
    from .state import HashCache

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

#: Hash-cache scheme version (#92). Bump when the digest algorithm or the semantics of
#: what bytes a digest covers change, so a persisted digest is never served under a new
#: scheme. It rides inside the cache ``mode_key`` alongside the algorithm name and the
#: sample size, which changes automatically if :data:`_SAMPLE` changes.
_HASH_CACHE_VERSION = 1


def _cache_mode_key(mode: str) -> str:
    """The cache-key discriminator for ``mode`` (#92).

    Folds in the digest algorithm, the sampled-region size, and a scheme version so a
    cached digest is only ever reused for an identical hashing scheme — a ``partial``
    row never satisfies a ``full`` lookup, and re-tuning :data:`_SAMPLE` or the digest
    invalidates every prior row.
    """

    return f"sha256|{mode}|sample={_SAMPLE}|v{_HASH_CACHE_VERSION}"


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
) -> Tuple[Optional[Path], Optional[Tuple[int, int, int, int]], Optional[str]]:
    """Translate + safety-check one Plex part path, mirroring the reclaim path.

    Returns ``(container_path, identity, None)`` when the file is a real, in-root
    regular file whose on-disk size matches what Plex reported, else
    ``(None, None, reason)``. Fail-closed on every branch: unmapped path,
    missing/unreadable file, symlink or non-regular file, a realpath that escapes the
    mapped root, or a size that drifted since the report.

    ``identity`` is ``(size, mtime_ns, ctime_ns, ino)`` and feeds the hash-cache
    fingerprint (#92). It deliberately includes **``ctime_ns`` and ``ino``** on top of
    size/mtime: an overwrite that preserves size *and* mtime (``cp -p``, ``rsync -t``,
    a restore, or a coarse-mtime filesystem) still bumps ``ctime`` — which userspace
    cannot forge — and an atomic replace-by-rename changes the inode, so a stale digest
    is not served for genuinely different bytes. All four fields come free from the
    single ``lstat`` already performed here.
    """

    mapped = map_media_path(plex_path, path_map)
    if mapped is None:
        return None, None, f"path not mapped by WEB_MEDIA_PATH_MAP: {plex_path}"
    container_path, container_prefix = mapped

    try:
        info = os.lstat(container_path)
    except OSError as exc:
        return None, None, f"not readable: {container_path} ({exc.__class__.__name__})"
    if not stat_mod.S_ISREG(info.st_mode):
        return None, None, f"not a regular file (symlink or directory?): {container_path}"

    real_path = Path(os.path.realpath(container_path))
    real_root = Path(os.path.realpath(container_prefix))
    try:
        real_path.relative_to(real_root)
    except ValueError:
        return None, None, f"resolved path escapes the media root: {container_path}"

    if info.st_size != expected_size:
        return None, None, (
            f"size changed since the report ({info.st_size} on disk != {expected_size} "
            f"reported): {container_path}"
        )
    identity = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
    return container_path, identity, None


def _verify_readable(resolved: Sequence[Tuple[Path, Tuple[int, int, int, int]]]) -> Optional[str]:
    """Confirm every resolved part is still openable, or return a fail-closed reason.

    Used only on a cache hit (#92): it opens and immediately closes each part so a copy
    whose bytes are no longer readable is refused as unhashable — mirroring the
    uncached read path's ``open()`` — while reading **zero** content bytes, so the
    whole-file read the cache exists to avoid stays skipped.
    """

    for container_path, _identity in resolved:
        try:
            with open(container_path, "rb"):
                pass
        except OSError as exc:
            return f"read failed: {container_path} ({exc.__class__.__name__})"
    return None


def _hash_copy(
    parts: Sequence[MediaCopy],
    path_map: Sequence[Tuple[Path, Path]],
    mode: str,
    cache: Optional["HashCache"] = None,
) -> CopyHash:
    """Hash one logical copy: its ordered parts fed into a single digest.

    The digest is over content bytes only (no framing), so a single-part ``full``
    digest equals a plain ``sha256`` of the file — a clean, testable property. Any
    part that fails :func:`_resolve_part` makes the whole copy unhashable.

    Every part is resolved and safety-checked (path map, regular-file, symlink,
    root-escape, on-disk size) up front. With a ``cache`` (#92), the resolved
    fingerprint (each part's on-disk path plus ``size``/``mtime_ns``/``ctime_ns``/``ino``)
    is looked up before the file is read end-to-end: a hit returns the stored digest,
    skipping the expensive byte read but **not** the safety checks — including a fresh
    ``open()`` per part so a copy that has become unreadable (an ancestor-dir/ACL/LSM
    permission change that left size/mtime/ctime untouched) fails closed to unhashable
    exactly as the uncached path would, rather than being trusted as confirmed. A miss
    reads and hashes as before, then stores the result.
    """

    topology = tuple(part.size for part in parts)
    resolved: List[Tuple[Path, Tuple[int, int, int, int]]] = []  # (path, identity)
    for part in parts:
        container_path, identity, error = _resolve_part(part.file, path_map, part.size)
        if container_path is None or identity is None:
            # _resolve_part always pairs a failed resolution with a reason; fall back to
            # a generic one so a warning is never formatted over a bare ``None``.
            return CopyHash(
                topology=topology,
                digest=None,
                error=error or f"could not resolve part: {part.file}",
            )
        resolved.append((container_path, identity))

    copy_key = "\x00".join(str(path) for path, _identity in resolved)
    fingerprint = "\x00".join(
        ":".join(str(field) for field in identity) for _path, identity in resolved
    )
    mode_key = _cache_mode_key(mode)
    if cache is not None:
        cached = cache.get(copy_key, mode_key, fingerprint)
        if cached is not None:
            readability_error = _verify_readable(resolved)
            if readability_error is not None:
                return CopyHash(topology=topology, digest=None, error=readability_error)
            return CopyHash(topology=topology, digest=cached, error=None)

    digest = hashlib.sha256()
    for container_path, (size, _mtime, _ctime, _ino) in resolved:
        try:
            with open(container_path, "rb") as handle:
                for offset, length in hash_regions(size, mode):
                    handle.seek(offset)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(_READ_BLOCK, remaining))
                        if not chunk:
                            # EOF before the expected bytes: the file shrank/changed
                            # between the size check and this read (a copy still being
                            # written or replaced). Fail closed as unhashable rather
                            # than return a digest over the prefix, which would risk a
                            # false confirmed/different verdict on a partial read.
                            return CopyHash(
                                topology=topology,
                                digest=None,
                                error=(
                                    f"file changed during hashing (short read): {container_path}"
                                ),
                            )
                        digest.update(chunk)
                        remaining -= len(chunk)
        except OSError as exc:
            return CopyHash(
                topology=topology,
                digest=None,
                error=f"read failed: {container_path} ({exc.__class__.__name__})",
            )
    result = digest.hexdigest()
    if cache is not None:
        cache.put(copy_key, mode_key, fingerprint, result)
    return CopyHash(topology=topology, digest=result, error=None)


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


@dataclass(frozen=True)
class _Comparison:
    """How a set of hashed copies compared — the single rule both callers share.

    ``status`` is the all-or-nothing verdict (``confirmed`` / ``sample-match`` when every
    copy agrees, ``different`` when they do not, ``unhashable`` when no verdict is
    possible). ``redundant_count`` additionally counts the copies that share a digest
    with at least one sibling, which survives a partial disagreement: three copies where
    two match and one differs are ``different`` overall yet hold two redundant copies.

    ``unreadable`` carries each failed copy's reason and ``incomparable`` marks differing
    part layouts — the two distinct roads to ``unhashable``, kept apart only so a caller
    can word a warning; both mean "nothing was proven".
    """

    status: str
    redundant_count: int
    unreadable: Tuple[str, ...] = ()
    incomparable: bool = False


def _compare_copies(hashes: Sequence[CopyHash], mode: str) -> _Comparison:
    """Decide whether hashed copies are the same bytes (#9, #93).

    The one place the comparability rule lives, so an ``identical`` group
    (:func:`_confirm_one`) and an ``upgrade``'s same-size bucket
    (:func:`_confirm_upgrade`) can never drift into disagreeing about the same pair of
    files. Fail-closed: any copy that could not be read, or copies split into part
    layouts that cannot be compared without guessing, yield ``unhashable`` and prove
    nothing — never a false ``different`` for the same content stored differently.
    """

    unreadable = tuple(
        copy_hash.error or "could not be hashed"
        for copy_hash in hashes
        if copy_hash.digest is None
    )
    if unreadable:
        return _Comparison(UNHASHABLE, 0, unreadable=unreadable)
    if len({copy_hash.topology for copy_hash in hashes}) > 1:
        return _Comparison(UNHASHABLE, 0, incomparable=True)
    clusters = Counter(copy_hash.digest for copy_hash in hashes)
    redundant = sum(count for count in clusters.values() if count >= 2)
    status = _confirm_status(mode) if len(clusters) == 1 else DIFFERENT
    return _Comparison(status, redundant)


def _confirm_one(
    group: DuplicateGroup,
    path_map: Sequence[Tuple[Path, Path]],
    mode: str,
    cache: Optional["HashCache"] = None,
) -> Tuple[DuplicateGroup, Optional[str]]:
    """Hash one ``identical`` group and return it re-tagged plus an optional warning."""

    pairs = dedupe.rank_copies_with_parts(group)
    hashes = [_hash_copy(parts, path_map, mode, cache) for _logical, parts in pairs]
    comparison = _compare_copies(hashes, mode)

    if comparison.unreadable:
        unreadable = comparison.unreadable
        warning = (
            f"Content hash: '{group.title}' left size-only (unhashable): "
            f"{unreadable[0]}" + (f" (+{len(unreadable) - 1} more)" if len(unreadable) > 1 else "")
        )
        return replace(group, hash_status=UNHASHABLE), warning

    if comparison.incomparable:
        # Copies split into different part layouts cannot be compared byte-for-byte
        # without guessing; leave the group size-only rather than risk a false
        # different-content downgrade of same content stored differently.
        warning = (
            f"Content hash: '{group.title}' left size-only (copies have different "
            "part layouts; not comparable)"
        )
        return replace(group, hash_status=UNHASHABLE), warning

    if comparison.status != DIFFERENT:
        return replace(group, hash_status=comparison.status), None

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


def _confirm_upgrade(
    group: DuplicateGroup,
    path_map: Sequence[Tuple[Path, Path]],
    mode: str,
    cache: Optional["HashCache"] = None,
) -> DuplicateGroup:
    """Hash the same-size buckets inside one ``upgrade`` group and annotate it (#93).

    An ``upgrade``'s copies differ by design, so there is no group-wide verdict to
    reach — but two of them can still share an exact size and prove byte-identical.
    The logical (stack-merged) copies are bucketed by size and only buckets with two
    or more members are hashed: a size unique within the group can hold no redundancy,
    so its bytes are **never read**, keeping the added cost proportional to the
    redundancy actually present rather than to library size.

    Returns the group carrying its :class:`~models.HashBucket` verdicts and **nothing
    else changed** — same ``classification``, ``keeper``, ``reclaimable_bytes`` and
    ``reclaimable_keep_smallest``. This is the deliberate asymmetry with
    :func:`_confirm_one`: differing bytes inside an ``identical`` group mean Plex was
    wrong and one copy is not redundant (so the group must be protected), whereas
    differing bytes inside an ``upgrade`` are the *expected* case — a 720p and a 1080p
    that happen to share a size are obviously not the same bytes, and the reclaim was
    already keeping the best copy on merit, not on byte-identity. So these verdicts
    inform; they never protect and never reclaim. Emitting no warnings is part of that
    contract: the warning list is for things that bear on reclaim safety, and an
    unreadable upgrade member changes no outcome — its ``unhashable`` bucket status
    records it in the report instead.
    """

    buckets: Dict[int, List[Tuple[MediaCopy, List[MediaCopy]]]] = {}
    for logical, parts in dedupe.rank_copies_with_parts(group):
        buckets.setdefault(logical.size, []).append((logical, parts))

    verdicts: List[HashBucket] = []
    for size, members in buckets.items():  # insertion order == the group's best-first rank
        if len(members) < 2:
            continue
        hashes = [_hash_copy(parts, path_map, mode, cache) for _logical, parts in members]
        comparison = _compare_copies(hashes, mode)
        verdicts.append(
            HashBucket(
                size=size,
                status=comparison.status,
                copy_count=len(members),
                redundant_count=comparison.redundant_count,
                part_ids=tuple(logical.part_id for logical, _parts in members),
            )
        )
    if not verdicts:
        return group
    return replace(group, hash_buckets=tuple(verdicts))


def confirm_groups(
    groups: Sequence[DuplicateGroup],
    path_map: Sequence[Tuple[Path, Path]],
    mode: str,
    cache: Optional["HashCache"] = None,
) -> Tuple[List[DuplicateGroup], List[str]]:
    """Run the content-hash pass over analyzed groups.

    An ``identical`` group is confirmed or downgraded outright (:func:`_confirm_one`);
    an ``upgrade`` gets its same-size buckets annotated, changing nothing else
    (:func:`_confirm_upgrade`, #93). A ``mismatch`` — and an already-downgraded
    ``different-content`` — is returned untouched: it is protected from reclaim on
    identity grounds, so no hash verdict could make it safer. Returns the groups in the
    same order plus any warnings.

    Fail-closed when media is not reachable: with no path map, or with a map whose
    roots are all unmounted, the pass is skipped whole (one warning) and the report
    is served from Plex data alone — never crashing a read-only report over a missing
    mount.

    An optional ``cache`` (#92) is consulted per copy so an unchanged file is not
    re-read on a subsequent run. It is fail-open (a broken cache degrades to live
    hashing) and never opened for ``HASH_MODE=off`` — this function short-circuits
    before touching it.
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
        if group.classification == dedupe.IDENTICAL:
            confirmed, warning = _confirm_one(group, path_map, mode, cache)
            result.append(confirmed)
            if warning is not None:
                warnings.append(warning)
        elif group.classification == dedupe.UPGRADE:
            result.append(_confirm_upgrade(group, path_map, mode, cache))
        else:
            result.append(group)
    return result, warnings
