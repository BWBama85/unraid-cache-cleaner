"""Pure duplicate-analysis engine.

Turns raw :class:`~unraid_cache_cleaner.models.DuplicateGroup` records (from the
Plex client, #5) into ranked, classified groups with reclaimable-byte math. No
I/O — every function here is a pure transform over the models, so the whole
module is trivially unit-testable and safe to call from the read-only report.

The one hard safety rule: a ``mismatch`` group (Plex merged copies whose file
paths carry different external ids, i.e. probably different titles) is **never**
counted as reclaimable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Dict, List, Set

from .models import DedupeSummary, DuplicateGroup, MediaCopy, SectionSummary

LOGGER = logging.getLogger(__name__)

IDENTICAL = "identical"
UPGRADE = "upgrade"
MISMATCH = "mismatch"

#: Plex ``videoResolution`` value -> sortable rank. Higher is better; an unknown
#: or missing value ranks ``0`` and therefore sorts last.
RES_RANK: Dict[str, int] = {
    "4k": 2160,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "sd": 1,
}

# ``{imdb-tt1234567}`` / ``{tmdb-123}`` tags embedded in Plex file paths. Two
# distinct ids in one group mean Plex stacked different titles together.
_ID_RE = re.compile(r"\{(?:imdb-(tt\d+)|tmdb-(\d+))\}")


def resolution_rank(resolution: str) -> int:
    """Map a Plex resolution label to a sortable rank (unknown -> ``0``)."""

    key = resolution.strip().lower()
    if key.endswith("p") and key[:-1].isdigit():
        key = key[:-1]
    return RES_RANK.get(key, 0)


def copy_sort_key(copy: MediaCopy) -> tuple:
    """Best-first ordering key: ``(resolution_rank, bitrate, size)``.

    Size alone is misleading — a 1080p x265 encode can be smaller than a 720p
    copy — so resolution and bitrate lead the key.
    """

    return (resolution_rank(copy.resolution), copy.bitrate, copy.size)


def _merge_stacks(copies: tuple) -> List[MediaCopy]:
    """Collapse stacked parts into one logical copy each.

    Parts sharing a non-zero ``media_id`` belong to the same Plex ``Media``
    element (a title split across several files); they are one logical copy
    whose size is the sum of its parts, not a set of duplicates. ``media_id`` of
    ``0`` means ungrouped — each such copy stands on its own. First-appearance
    order is preserved so ranking and keeper selection are deterministic.
    """

    logical: List[MediaCopy] = []
    stack_index: Dict[int, int] = {}
    for copy in copies:
        if copy.media_id == 0:
            logical.append(copy)
            continue
        if copy.media_id in stack_index:
            idx = stack_index[copy.media_id]
            logical[idx] = replace(logical[idx], size=logical[idx].size + copy.size)
        else:
            stack_index[copy.media_id] = len(logical)
            logical.append(copy)
    return logical


def rank_copies(group: DuplicateGroup) -> List[MediaCopy]:
    """Return the group's logical copies sorted best-first."""

    return sorted(_merge_stacks(group.copies), key=copy_sort_key, reverse=True)


def _external_ids(group: DuplicateGroup) -> Set[str]:
    """Namespaced external ids parsed from every copy's file path."""

    ids: Set[str] = set()
    for copy in group.copies:
        for imdb, tmdb in _ID_RE.findall(str(copy.file)):
            ids.add(f"imdb:{imdb}" if imdb else f"tmdb:{tmdb}")
    return ids


def _all_same_res_and_size(copies: List[MediaCopy]) -> bool:
    first = copies[0]
    first_rank = resolution_rank(first.resolution)
    return all(
        resolution_rank(copy.resolution) == first_rank and copy.size == first.size
        for copy in copies[1:]
    )


def classify(group: DuplicateGroup) -> str:
    """Classify a group as ``mismatch`` / ``identical`` / ``upgrade``.

    ``mismatch`` wins outright: if two or more distinct external ids appear in
    the copies' paths the group is treated as different titles and protected
    from reclaim. Otherwise a group whose logical copies all share resolution
    and size is ``identical`` (redundant copies); anything else is an
    ``upgrade`` (a better copy supersedes a worse one).
    """

    if len(_external_ids(group)) >= 2:
        return MISMATCH
    logical = _merge_stacks(group.copies)
    if _all_same_res_and_size(logical):
        return IDENTICAL
    return UPGRADE


def reclaimable_bytes(group: DuplicateGroup) -> int:
    """Bytes freed by keeping the best copy (``0`` for a mismatch)."""

    if classify(group) == MISMATCH:
        return 0
    logical = rank_copies(group)
    if len(logical) < 2:
        return 0
    return sum(copy.size for copy in logical) - logical[0].size


def reclaimable_keep_smallest(group: DuplicateGroup) -> int:
    """Max-reclaim view: bytes freed by keeping the smallest copy.

    Still ``0`` for a mismatch — the safety rule applies to every reclaim view.
    """

    if classify(group) == MISMATCH:
        return 0
    logical = _merge_stacks(group.copies)
    if len(logical) < 2:
        return 0
    return sum(copy.size for copy in logical) - min(copy.size for copy in logical)


def analyze_group(group: DuplicateGroup) -> DuplicateGroup:
    """Return a copy of ``group`` with the analysis fields populated."""

    ranked = rank_copies(group)
    return replace(
        group,
        keeper=ranked[0] if ranked else None,
        classification=classify(group),
        reclaimable_bytes=reclaimable_bytes(group),
        reclaimable_keep_smallest=reclaimable_keep_smallest(group),
    )


def analyze(groups: List[DuplicateGroup]) -> List[DuplicateGroup]:
    """Analyze every real duplicate group.

    Groups that collapse to a single logical copy once stacks are merged are not
    duplicates and are dropped.
    """

    analyzed: List[DuplicateGroup] = []
    for group in groups:
        if len(_merge_stacks(group.copies)) < 2:
            continue
        analyzed.append(analyze_group(group))
    return analyzed


def summarize(groups: List[DuplicateGroup]) -> DedupeSummary:
    """Aggregate per-section (by ``kind``) and overall duplicate totals.

    Accepts raw or already-analyzed groups: it runs :func:`analyze` internally,
    so non-duplicates are dropped and reclaimable figures are recomputed from the
    copies. Reclaimable totals exclude ``mismatch`` groups by construction.
    """

    analyzed = analyze(groups)

    order: List[str] = []
    buckets: Dict[str, Dict[str, int]] = {}
    for group in analyzed:
        bucket = buckets.get(group.kind)
        if bucket is None:
            bucket = {
                "group_count": 0,
                "copy_count": 0,
                "identical_count": 0,
                "upgrade_count": 0,
                "mismatch_count": 0,
                "reclaimable_bytes": 0,
                "reclaimable_keep_smallest": 0,
            }
            buckets[group.kind] = bucket
            order.append(group.kind)
        bucket["group_count"] += 1
        bucket["copy_count"] += len(_merge_stacks(group.copies))
        bucket[f"{group.classification}_count"] += 1
        bucket["reclaimable_bytes"] += group.reclaimable_bytes
        bucket["reclaimable_keep_smallest"] += group.reclaimable_keep_smallest

    sections = tuple(
        SectionSummary(kind=kind, **buckets[kind]) for kind in order
    )
    return DedupeSummary(
        sections=sections,
        group_count=sum(section.group_count for section in sections),
        copy_count=sum(section.copy_count for section in sections),
        identical_count=sum(section.identical_count for section in sections),
        upgrade_count=sum(section.upgrade_count for section in sections),
        mismatch_count=sum(section.mismatch_count for section in sections),
        reclaimable_bytes=sum(section.reclaimable_bytes for section in sections),
        reclaimable_keep_smallest=sum(
            section.reclaimable_keep_smallest for section in sections
        ),
    )
