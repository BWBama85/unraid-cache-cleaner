"""Read-only Plex duplicate report orchestrator.

Wires the Plex client (#5) and the dedupe engine (#6) into a one-shot,
report-only flow that mirrors ``service.py``'s ``run_once -> write_report ->
log_report`` shape but never deletes anything:

1. resolve the video sections to scan (explicit ids or auto-detected),
2. fetch each section's duplicates and parse them into ``DuplicateGroup``s,
3. analyze them with :mod:`dedupe`,
4. emit a stable JSON report, a compact summary log line, and a
   human-readable, reclaimable-sorted table.

The printer is pure: :meth:`PlexDuplicateReporter.render_table` takes a
``DuplicateReport`` and returns a ``str``, so it is unit-testable without
capturing stdout.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Tuple

from . import arr, dedupe, hasher
from .arr import ArrClientError, RadarrClient, SonarrClient
from .config import Config
from .models import DuplicateGroup, DuplicateReport, MediaCopy, PlexSection
from .plex import PlexClient, build_duplicate_group

if TYPE_CHECKING:  # annotation only; HashCache is imported lazily in _open_hash_cache
    from .state import HashCache

LOGGER = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: Plex library ``type`` -> (duplicate-query item ``type``, ``DuplicateGroup.kind``).
#: ``1`` = movie, ``4`` = episode. Only these video library types are scanned; a
#: music/photo section (or an unknown one) is never treated as a video library.
#: Kept as one mapping so the item-type and kind can't drift out of lockstep.
_SECTION_SPEC = {"movie": (1, "movie"), "show": (4, "episode")}


def _fmt_gib(num_bytes: int) -> str:
    return f"{num_bytes / _GIB:.1f} GiB"


def _copy_json(
    copy: MediaCopy,
    parts: Sequence[MediaCopy],
    *,
    include_arr: bool = False,
) -> dict:
    payload = {
        "file": str(copy.file),
        "size": copy.size,
        "resolution": copy.resolution,
        "bitrate": copy.bitrate,
        # The Plex ``Media`` id this copy's parts share (0 = ungrouped). Surfaced
        # so a consumer can address one logical copy — and, with the per-part
        # ``part_id`` below, one physical file — as a stable delete target: the
        # web action layer (#34 Phase 2) routes a reclaim by ``{rating_key,
        # part_id}`` and must remove *all* parts of a stacked copy together.
        "media_id": copy.media_id,
        # Each physical file backing this copy with its own true size, so a
        # stacked multi-part copy (cd1/cd2 under one Plex media_id) exposes both
        # paths and their individual sizes instead of hiding the siblings behind
        # the first part's path and the summed size (#17). Always present and a
        # single element for an unstacked copy, so a consumer can read
        # ``copy["parts"]`` unconditionally. ``part_id`` is the Plex ``Part`` id —
        # the precise per-file delete target for #34 Phase 2. On an arr-enabled
        # run each part also carries its ``*arr`` file id (#61) so the web action
        # layer deletes a tracked copy by id (drift-safe, O(1)); it is per-part
        # (never one id spread across a stack) and ``null`` when not pinned.
        "parts": [_part_json(part, include_arr=include_arr) for part in parts],
    }
    # The copy-level arr fields are serialized only when the arr layer ran, so a
    # Plex-only report omits them (the base keys above, including media_id/part_id,
    # are always present).
    if include_arr:
        payload["association"] = copy.association
        payload["arr_tracked"] = copy.arr_tracked
    return payload


def _part_json(part: MediaCopy, *, include_arr: bool = False) -> dict:
    payload = {"part_id": part.part_id, "file": str(part.file), "size": part.size}
    # ``arr_file_id`` rides only on arr-enabled reports (``null`` unless this part
    # is a pinned tracked file), keeping the Plex-only part shape byte-identical.
    if include_arr:
        payload["arr_file_id"] = part.arr_file_id
    return payload


class PlexDuplicateReporter:
    """Generate a read-only Plex duplicate report.

    Takes its client via the constructor (like ``CleanerService``) so tests can
    inject a fake, and an injectable ``clock`` so ``generated_at`` is
    deterministic under test — required for the byte-identical JSON guarantee,
    which ``sort_keys`` alone does not provide.
    """

    def __init__(
        self,
        config: Config,
        client: PlexClient,
        *,
        radarr_client: Optional[RadarrClient] = None,
        sonarr_client: Optional[SonarrClient] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.client = client
        self.radarr_client = radarr_client
        self.sonarr_client = sonarr_client
        self.clock = clock
        # Per-report memo of each group's best-first ranking (see _ranked_pairs),
        # keyed by object identity and scoped to one report (via _begin_render) so
        # the JSON, log, and table outputs of a single command share one ranking
        # per group instead of re-ranking in each.
        self._rank_cache: Dict[
            int, Tuple[DuplicateGroup, List[Tuple[MediaCopy, List[MediaCopy]]]]
        ] = {}
        self._rank_cache_report: Optional[DuplicateReport] = None

    @property
    def _arr_enabled(self) -> bool:
        """The association layer runs when at least one *arr client is present."""

        return self.radarr_client is not None or self.sonarr_client is not None

    def _resolve_sections(
        self, overrides: Optional[Sequence[str]]
    ) -> Tuple[List[PlexSection], List[str]]:
        """Pick the sections to scan and collect any skip warnings.

        Explicit ids (``--section`` / ``PLEX_SECTIONS``) win; an id that is
        unknown or not a video library is skipped with a warning rather than
        crashing. With no ids given, every video section is auto-detected.
        """

        all_sections = self.client.fetch_sections()
        requested = tuple(overrides) if overrides else self.config.plex_sections
        if not requested:
            auto = [s for s in all_sections if s.type in _SECTION_SPEC]
            return auto, []

        by_key = {section.key: section for section in all_sections}
        resolved: List[PlexSection] = []
        warnings: List[str] = []
        seen: set = set()
        for key in requested:
            key = str(key)
            if key in seen:  # a repeated id must not scan (and double-count) twice
                continue
            seen.add(key)
            section = by_key.get(key)
            if section is None:
                warnings.append(f"Section {key} not found on Plex; skipping")
                continue
            if section.type not in _SECTION_SPEC:
                warnings.append(
                    f"Section {key} ({section.title}, type={section.type}) "
                    "is not a video library; skipping"
                )
                continue
            resolved.append(section)
        return resolved, warnings

    def generate(
        self, section_overrides: Optional[Sequence[str]] = None
    ) -> DuplicateReport:
        """Scan the resolved sections and return an analyzed report."""

        sections, warnings = self._resolve_sections(section_overrides)

        raw_groups: List[DuplicateGroup] = []
        for section in sections:
            item_type, kind = _SECTION_SPEC[section.type]
            for item in self.client.fetch_duplicates(section.key, item_type):
                group = build_duplicate_group(item, kind)
                if group is not None:
                    raw_groups.append(group)

        analyzed = dedupe.analyze(raw_groups)

        # Optional content-hash pass (#9): confirm/downgrade the ``identical`` groups
        # BEFORE sorting and summarizing, so a downgrade (different-content ⇒
        # reclaimable 0) reorders the table and is reflected in the totals. The
        # summary is computed with ``summarize_analyzed`` (not ``summarize``) precisely
        # so re-analysis does not wipe the hash verdicts.
        hash_enabled = self.config.hash_mode != hasher.HASH_OFF
        if hash_enabled:
            cache = self._open_hash_cache()
            try:
                analyzed, hash_warnings = hasher.confirm_groups(
                    analyzed,
                    self.config.web_media_path_map,
                    self.config.hash_mode,
                    cache=cache,
                )
            finally:
                if cache is not None:
                    cache.close()
            warnings.extend(hash_warnings)

        analyzed.sort(key=self._group_sort_key)

        if self._arr_enabled:
            radarr_index, sonarr_index = self._build_arr_indexes(warnings)
            analyzed = arr.annotate(analyzed, radarr_index, sonarr_index)

        summary = dedupe.summarize_analyzed(analyzed)

        return DuplicateReport(
            generated_at=self.clock(),
            sections=tuple(sections),
            groups=analyzed,
            total_groups=summary.group_count,
            total_copies=summary.copy_count,
            reclaimable_bytes=summary.reclaimable_bytes,
            summary=summary,
            warnings=warnings,
            arr_enabled=self._arr_enabled,
            hash_enabled=hash_enabled,
        )

    def _open_hash_cache(self) -> Optional["HashCache"]:
        """Open the persistent hash cache for this run, or ``None`` when disabled (#92).

        Called only when the hash pass runs, so the sidecar DB is never created for
        ``HASH_MODE=off``. Construction is fail-open — a broken/unwritable cache yields
        a disabled instance that behaves as a no-op — so the caller can always close it.
        """

        if not self.config.hash_cache_enabled:
            return None
        from .state import HashCache

        return HashCache(self.config.hash_cache_path)

    def _build_arr_indexes(
        self, warnings: List[str]
    ) -> Tuple[Dict[str, Dict[str, Optional[int]]], Dict[str, List[Optional[int]]]]:
        """Fetch the Radarr/Sonarr tracked indexes, degrading gracefully.

        An unconfigured client contributes an empty index; a configured but
        unreachable one logs a warning and also contributes an empty index — so
        an ``*arr`` outage never fails the read-only report, it just leaves that
        kind ``unknown``. Each index now carries the tracked files' ``*arr`` ids
        (#61), captured in the same fetch, so the report can serialize them.
        """

        radarr_index: Dict[str, Dict[str, Optional[int]]] = {}
        sonarr_index: Dict[str, List[Optional[int]]] = {}
        if self.radarr_client is not None:
            try:
                radarr_index = self.radarr_client.fetch_tracked_index()
            except ArrClientError as exc:
                warnings.append(f"Radarr association skipped: {exc}")
        if self.sonarr_client is not None:
            try:
                sonarr_index = self.sonarr_client.fetch_tracked_index()
            except ArrClientError as exc:
                warnings.append(f"Sonarr association skipped: {exc}")
        return radarr_index, sonarr_index

    def _summary(self, report: DuplicateReport):
        """The report's precomputed summary, recomputed only if absent.

        Recomputes with ``summarize_analyzed`` (the report's groups are already
        analyzed and possibly hash-tagged), so a fallback recompute never re-runs
        ``analyze`` and wipes a content-hash downgrade."""

        if report.summary is not None:
            return report.summary
        return dedupe.summarize_analyzed(report.groups)

    @staticmethod
    def _group_sort_key(group: DuplicateGroup) -> Tuple[int, str, str]:
        # reclaimable desc, then a stable tiebreak so two runs on the same input
        # serialize byte-identically.
        return (-group.reclaimable_bytes, group.kind, group.rating_key)

    def _begin_render(self, report: DuplicateReport) -> None:
        """Point the ranking memo at ``report``, clearing it only when the report
        changes.

        A single command renders one report three times — ``write_report`` ->
        ``build_payload``, then ``log_report``, then ``render_table`` — so scoping
        the memo to the report object (not resetting it per output method) ranks
        every group exactly once across all three outputs (#19). A later,
        different report starts fresh, so no stale ranking is ever reused."""

        if self._rank_cache_report is not report:
            self._rank_cache = {}
            self._rank_cache_report = report

    def _ranked_pairs(
        self, group: DuplicateGroup
    ) -> List[Tuple[MediaCopy, List[MediaCopy]]]:
        """``dedupe.rank_copies_with_parts(group)``, memoized per report.

        The JSON body, the reclaimable rows (count + #48 part sub-rows), the arr
        tag, the arr-tracked rows, and the arr-tracked count each need a group's
        best-first ranking; without memoization one group is stack-merged and
        sorted five-plus times per render, and re-ranked again by each of the
        three command outputs (#19). Keyed by object identity — ``DuplicateGroup``
        carries an unhashable ``external_ids`` dict *and* a Plex ``rating_key``
        that is ``""`` when the item omits it (so it can't key uniquely) — with a
        strong reference held so a live entry's ``id()`` can never be recycled by
        another group, re-validated against the stored group for defence in depth.
        """

        cached = self._rank_cache.get(id(group))
        if cached is not None and cached[0] is group:
            return cached[1]
        pairs = dedupe.rank_copies_with_parts(group)
        self._rank_cache[id(group)] = (group, pairs)
        return pairs

    def _group_json(
        self, group: DuplicateGroup, *, include_arr: bool = False, include_hash: bool = False
    ) -> dict:
        pairs = self._ranked_pairs(group)

        if not dedupe.is_reclaimable(group.classification):
            # A protected group's copies are the *physical* files (stacks NOT
            # merged) so an operator reviewing the conflict sees each conflicting
            # path and its true size, not one stack-merged copy at the summed size:
            # a mismatch (#25) or a hash-proven different-content group (#9), which
            # the CLI table also renders per physical file. Each is its own part.
            copies = [
                _copy_json(copy, [copy], include_arr=include_arr)
                for copy in dedupe.rank_physical_copies(group)
            ]
        else:
            copies = [
                _copy_json(logical, parts, include_arr=include_arr)
                for logical, parts in pairs
            ]

        # The keeper is, by construction, the best-ranked logical copy — i.e. the
        # first pair — so pair it with that pair's physical parts for the
        # per-file breakdown (#17). ``rank_copies_with_parts`` sorts identically
        # to the ``rank_copies`` call that set ``group.keeper``.
        keeper_json = None
        if group.keeper is not None and pairs:
            keeper_json = _copy_json(group.keeper, pairs[0][1], include_arr=include_arr)

        payload = {
            "rating_key": group.rating_key,
            "title": group.title,
            "kind": group.kind,
            "classification": group.classification,
            "reclaimable_bytes": group.reclaimable_bytes,
            "keeper": keeper_json,
            "copies": copies,
        }
        # ``hash_status`` rides only on a hashed report, so an ``HASH_MODE=off`` report
        # keeps its exact prior shape (the key is absent, not empty).
        if include_hash:
            payload["hash_status"] = group.hash_status
            # Same-size bucket verdicts (#93) exist only on an ``upgrade`` that has one,
            # so the key is omitted rather than serialized as an empty list on every
            # other group — absent and ``[]`` would mean the same thing ("no same-size
            # bucket to report"), and omitting keeps the common group lean.
            if group.hash_buckets:
                payload["hash_buckets"] = [
                    {
                        "size": bucket.size,
                        "status": bucket.status,
                        "copy_count": bucket.copy_count,
                        "redundant_count": bucket.redundant_count,
                        "part_ids": list(bucket.part_ids),
                    }
                    for bucket in group.hash_buckets
                ]
        return payload

    def build_payload(self, report: DuplicateReport) -> dict:
        """Return the stable JSON payload for ``report`` (also used by tests)."""

        self._begin_render(report)
        summary = self._summary(report)
        include_arr = report.arr_enabled
        include_hash = report.hash_enabled
        payload: dict = {
            "generated_at": report.generated_at,
            "plex_url": self.config.plex_url,
            "sections": [
                {"key": section.key, "type": section.type, "title": section.title}
                for section in report.sections
            ],
            "totals": {
                "duplicate_group_count": summary.group_count,
                "reclaimable_bytes": summary.reclaimable_bytes,
                "reclaimable_bytes_keep_smallest": summary.reclaimable_keep_smallest,
                "mismatch_count": summary.mismatch_count,
            },
            "groups": [
                self._group_json(group, include_arr=include_arr, include_hash=include_hash)
                for group in report.groups
            ],
            "warnings": report.warnings,
            "errors": report.errors,
        }
        # The arr totals/flag are added only when the arr layer ran, so a Plex-only
        # report omits them.
        if include_arr:
            payload["arr_enabled"] = True
            payload["totals"]["arr_tracked_reclaimable_count"] = self._arr_reclaimable_tracked_count(
                report
            )
        # The hash totals/flag ride only on a hashed report, so an ``HASH_MODE=off``
        # report keeps its exact prior shape.
        if include_hash:
            payload["hash_enabled"] = True
            payload["hash_mode"] = self.config.hash_mode
            payload["totals"]["hash_confirmed_count"] = summary.hash_confirmed_count
            payload["totals"]["hash_sample_match_count"] = summary.hash_sample_match_count
            payload["totals"]["hash_unhashable_count"] = summary.hash_unhashable_count
            payload["totals"]["different_content_count"] = summary.different_count
            payload["totals"]["hash_redundant_upgrade_count"] = (
                summary.hash_redundant_upgrade_count
            )
        return payload

    def _reclaim_candidates(self, group: DuplicateGroup) -> List[MediaCopy]:
        """The copies a reclaim would delete: every logical copy but the keeper."""

        return [logical for logical, _ in self._ranked_pairs(group)][1:]

    def _reclaim_candidates_with_parts(
        self, group: DuplicateGroup
    ) -> List[Tuple[MediaCopy, List[MediaCopy]]]:
        """Reclaim candidates paired with their physical parts, so a stacked
        tracked copy can be listed as each of its part files (#17)."""

        return self._ranked_pairs(group)[1:]

    def _arr_reclaimable_tracked_count(self, report: DuplicateReport) -> int:
        """Count reclaim-candidate copies an *arr tracks (delete ⇒ re-download).

        Skips every protected group (mismatch *and* hash-proven different-content),
        so the count matches the reclaimable/arr-tracked sections — a downgraded
        different-content group is never counted as arr-tracked reclaimable."""

        count = 0
        for group in report.groups:
            if not dedupe.is_reclaimable(group.classification):
                continue
            count += sum(
                1 for copy in self._reclaim_candidates(group) if copy.association == arr.TRACKED
            )
        return count

    def write_report(self, report: DuplicateReport) -> None:
        """Write the duplicate report as stable, ``sort_keys`` JSON.

        Written atomically (unique temp file in the same directory + ``os.replace``)
        so a concurrent reader — notably the read-only web viewer (#34) polling
        this file while a scan rewrites it — never observes a truncated or
        half-written report. ``mkstemp`` gives each writer a unique scratch name,
        so two writers over one ``/config`` volume (even separate containers, each
        PID 1) can never share a temp path or unlink each other's; ``os.replace``
        is atomic within a filesystem, and the temp stays colocated with the
        target to remain on it.
        """

        payload = self.build_payload(report)
        target = self.config.plex_duplicate_report_path
        data = json.dumps(payload, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            # mkstemp creates the temp 0600 (owner-only); publishing that would
            # make the report unreadable to a separate web-container/host reader.
            # Preserve the existing report's mode, or fall back to a readable
            # default for the first write.
            try:
                mode = stat.S_IMODE(target.stat().st_mode)
            except FileNotFoundError:
                mode = 0o644
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        finally:
            # After a successful replace the temp is already consumed; this only
            # cleans up our own unique scratch on the write/replace failure path.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def log_report(self, report: DuplicateReport) -> None:
        """Emit one compact summary line mirroring ``service.log_report``.

        Skip warnings (e.g. a bad ``--section`` id) are logged first so they
        surface even when the scan finds no duplicates and returns early.
        """

        self._begin_render(report)
        for warning in report.warnings:
            LOGGER.warning("%s", warning)
        summary = self._summary(report)
        if summary.group_count == 0:
            scanned = ", ".join(section.title for section in report.sections)
            LOGGER.info("Plex duplicates: none found in sections %s", scanned or "(none)")
            return
        arr_note = ""
        if report.arr_enabled:
            arr_note = f" arr_tracked={self._arr_reclaimable_tracked_count(report)}"
        hash_note = ""
        if report.hash_enabled:
            hash_note = (
                f" hash={self.config.hash_mode}"
                f" confirmed={summary.hash_confirmed_count}"
                f" sample_match={summary.hash_sample_match_count}"
                f" different={summary.different_count}"
                f" unhashable={summary.hash_unhashable_count}"
                f" redundant_upgrades={summary.hash_redundant_upgrade_count}"
            )
        LOGGER.info(
            "Plex duplicates: sections=%s groups=%s reclaimable=%s mismatches=%s%s%s",
            len(report.sections),
            summary.group_count,
            _fmt_gib(summary.reclaimable_bytes),
            summary.mismatch_count,
            hash_note,
            arr_note,
        )

    def render_table(
        self, report: DuplicateReport, *, limit: Optional[int] = None
    ) -> str:
        """Render the human-readable, reclaimable-sorted table (pure)."""

        self._begin_render(report)
        summary = self._summary(report)
        scanned = ", ".join(
            f"{section.title} (#{section.key})" for section in report.sections
        ) or "(none)"

        if summary.group_count == 0:
            lines = [f"No duplicate media found in sections: {scanned}."]
            lines.extend(f"  warning: {warning}" for warning in report.warnings)
            return "\n".join(lines)

        lines: List[str] = [
            "Plex duplicate report",
            f"  Sections scanned: {scanned}",
            (
                f"  Duplicate groups: {summary.group_count}"
                f"   Reclaimable: {_fmt_gib(summary.reclaimable_bytes)}"
                f"   Mismatches: {summary.mismatch_count}"
            ),
        ]
        lines.extend(f"  warning: {warning}" for warning in report.warnings)
        lines.append("")

        # Every reclaimable group is listed, including any whose reclaimable bytes
        # are 0 (e.g. copies Plex reports without a size) — so the section rows always
        # account for every group the header counts. The predicate excludes both
        # ``mismatch`` and (when the hash pass ran) ``different-content``.
        reclaimable = [
            group for group in report.groups if dedupe.is_reclaimable(group.classification)
        ]
        reclaimable.sort(key=self._group_sort_key)
        total = sum(group.reclaimable_bytes for group in reclaimable)
        lines.append(
            f"Reclaimable (safe) - {_fmt_gib(total)} across {len(reclaimable)} groups"
        )
        lines.extend(
            self._render_reclaimable_rows(
                reclaimable, limit, arr_enabled=report.arr_enabled, hash_enabled=report.hash_enabled
            )
        )
        lines.append("")

        mismatches = [
            group for group in report.groups if group.classification == dedupe.MISMATCH
        ]
        mismatches.sort(key=lambda group: (group.kind, group.title, group.rating_key))
        lines.append(
            f"Review - possible mismatches (not counted) - {len(mismatches)} groups"
        )
        lines.extend(self._render_mismatch_rows(mismatches, limit))
        lines.append("")

        # Different-content review section, only when the content-hash pass ran (#9):
        # groups Plex reported as same-size copies that hashing proved differ. Excluded
        # from reclaimable above; surfaced here so the exclusion is never silent.
        if report.hash_enabled:
            different = [
                group for group in report.groups if group.classification == dedupe.DIFFERENT
            ]
            different.sort(key=lambda group: (group.kind, group.title, group.rating_key))
            lines.append(
                f"Review - different content (hash mismatch, excluded) - {len(different)} groups"
            )
            lines.extend(self._render_mismatch_rows(different, limit))
            lines.append("")

        lines.append("[!] arr-tracked (Radarr/Sonarr)")
        lines.extend(self._render_arr_rows(report, reclaimable, limit))
        return "\n".join(lines)

    def _group_arr_tag(self, group: DuplicateGroup) -> str:
        """Trailing tag warning that a reclaim of ``group`` is not plain-``rm`` safe.

        ``[arr:tracked]`` when a to-be-deleted copy is *arr-tracked (deleting it
        re-downloads); ``[arr:?]`` when one is ``unknown``; empty when every
        reclaim candidate is confirmed ``untracked``.
        """

        candidates = self._reclaim_candidates(group)
        if any(copy.association == arr.TRACKED for copy in candidates):
            return "  [arr:tracked]"
        if any(copy.association == arr.UNKNOWN for copy in candidates):
            return "  [arr:?]"
        return ""

    def _render_reclaimable_rows(
        self,
        groups: List[DuplicateGroup],
        limit: Optional[int],
        *,
        arr_enabled: bool = False,
        hash_enabled: bool = False,
    ) -> List[str]:
        if not groups:
            return ["  (none)"]
        shown = groups if limit is None else groups[:limit]
        rows: List[str] = []
        for group in shown:
            pairs = self._ranked_pairs(group)
            keeper_res = (group.keeper.resolution or "?") if group.keeper else "?"
            hash_tag = self._hash_tag(group) if hash_enabled else ""
            arr_tag = self._group_arr_tag(group) if arr_enabled else ""
            rows.append(
                f"  {_fmt_gib(group.reclaimable_bytes):>10}  "
                f"{group.classification:<9} {group.kind:<7} "
                f"keep={keeper_res:<5} copies={len(pairs)}  {group.title}{hash_tag}{arr_tag}"
            )
            rows.extend(self._reclaimable_part_rows(pairs, arr_enabled=arr_enabled))
        rows.extend(self._truncation_note(len(groups), len(shown)))
        return rows

    def _hash_tag(self, group: DuplicateGroup) -> str:
        """Per-row content-hash verdict tag: confirmed / sampled / unverified (#9),
        or an ``upgrade``'s same-size redundancy (#93).

        The group-wide verdict wins when present (only an ``identical`` group has one).
        Otherwise an ``upgrade`` holding redundant same-size copies is tagged with how
        many — the actionable half of the bucket pass. The tag is graded by the run's
        ``HASH_MODE`` (``redundant`` under ``full``, ``redundant-sampled`` under
        ``partial``), because that is what decides whether a match is proof; tagging a
        sampled match the same as a proven one would claim evidence the pass never
        gathered, which is exactly the distinction the ``[hash:confirmed]`` /
        ``[hash:sample-match]`` tags above keep.

        A bucket that merely proved *different* gets no tag: inside an upgrade that is
        the expected case (a 720p and a 1080p sharing a size are obviously different
        bytes), so tagging it would mark almost every upgrade while saying nothing. The
        full per-bucket detail is in the JSON report either way.
        """

        tag = {
            hasher.CONFIRMED: "  [hash:confirmed]",
            hasher.SAMPLE_MATCH: "  [hash:sample-match]",
            hasher.UNHASHABLE: "  [hash:unhashable]",
        }.get(group.hash_status, "")
        if tag:
            return tag
        redundant = dedupe.redundant_bucket_copies(group)
        if not redundant:
            return ""
        if self.config.hash_mode == hasher.HASH_FULL:
            return f"  [hash:redundant={redundant}]"
        return f"  [hash:redundant-sampled={redundant}]"

    @staticmethod
    def _reclaimable_part_rows(
        pairs: List[Tuple[MediaCopy, List[MediaCopy]]],
        *,
        arr_enabled: bool = False,
    ) -> List[str]:
        """Indented part sub-rows for each *stacked* reclaim candidate (#48, #56).

        The compact reclaimable summary hides which physical files a stacked
        reclaim candidate is made of; this lists each part at its true size so an
        operator sees the same file->size fidelity the JSON already provides. Only
        non-keeper copies (``pairs[1:]``) with more than one part get sub-rows, so
        the common single-file case is unchanged.

        On an ``*arr``-enabled run a *tracked* candidate is skipped here — its parts
        already appear (with the same fidelity) in the arr-tracked section, so
        listing them again would double-print the copy. Untracked/unknown stacked
        candidates are still listed, so enabling ``*arr`` no longer strips the
        reclaimable file->size breakdown from copies the ``*arr`` does not track
        (#56). On a Plex-only run (``arr_enabled=False``) every stacked candidate is
        listed, exactly as before.
        """

        rows: List[str] = []
        for logical, parts in pairs[1:]:
            if arr_enabled and logical.association == arr.TRACKED:
                continue
            if len(parts) > 1:
                for part in parts:
                    rows.append(
                        f"      {_fmt_gib(part.size):>10}  "
                        f"{(logical.resolution or '?'):<6} {part.file}"
                    )
        return rows

    def _render_arr_rows(
        self,
        report: DuplicateReport,
        reclaimable: List[DuplicateGroup],
        limit: Optional[int],
    ) -> List[str]:
        """Render the arr-tracked section: reclaim candidates an *arr tracks.

        Lists the redundant copies (non-keeper) that Radarr/Sonarr tracks — these
        would re-download if you just delete the file, so remove them via the
        ``*arr`` instead. Untracked reclaim candidates are the safe common case
        and are not repeated here.
        """

        if not report.arr_enabled:
            return [
                "  Not configured - set RADARR_URL/RADARR_API_KEY or "
                "SONARR_URL/SONARR_API_KEY to flag copies that re-download when deleted."
            ]

        flagged: List[Tuple[DuplicateGroup, List[Tuple[MediaCopy, List[MediaCopy]]]]] = []
        unknown_count = 0
        for group in reclaimable:
            candidates = self._reclaim_candidates_with_parts(group)
            tracked = [
                (logical, parts)
                for logical, parts in candidates
                if logical.association == arr.TRACKED
            ]
            unknown_count += sum(
                1 for logical, _ in candidates if logical.association == arr.UNKNOWN
            )
            if tracked:
                flagged.append((group, tracked))
        if not flagged:
            # "safe" is only honest when nothing is unconfirmed: unknown reclaim
            # candidates (an *arr outage, or a TV copy whose filename didn't
            # match) are tagged [arr:?] above and must not be called safe.
            if unknown_count:
                return [
                    f"  No *arr-tracked reclaimable copies, but {unknown_count} are "
                    "unconfirmed ([arr:?] above) - verify those before deleting."
                ]
            return ["  (no reclaimable copy is *arr-tracked - all safe to delete)"]

        shown = flagged if limit is None else flagged[:limit]
        rows: List[str] = []
        for group, tracked in shown:
            service = tracked[0][0].arr_tracked or "*arr"
            rows.append(f"  {group.kind:<7} {group.title}  (tracked by {service})")
            # List each physical part of a tracked copy at its own size, so a
            # stacked release shows every file that must be removed via the *arr,
            # not just the first part at the summed size (#17).
            for logical, parts in tracked:
                for part in parts:
                    rows.append(
                        f"      {_fmt_gib(part.size):>10}  "
                        f"{(logical.resolution or '?'):<6} {part.file}"
                    )
        rows.extend(self._truncation_note(len(flagged), len(shown)))
        rows.append(
            "  Delete these via Radarr/Sonarr (or unmonitor first) or they re-download."
        )
        return rows

    def _render_mismatch_rows(
        self, groups: List[DuplicateGroup], limit: Optional[int]
    ) -> List[str]:
        if not groups:
            return ["  (none)"]
        shown = groups if limit is None else groups[:limit]
        rows: List[str] = []
        for group in shown:
            rows.append(f"  {group.kind:<7} {group.title}")
            # Physical copies (stacks NOT merged): show each conflicting file at
            # its own size so a mis-stacked pair (cd1/cd2 under one media_id)
            # surfaces both paths, not a single summed row (#25).
            for copy in dedupe.rank_physical_copies(group):
                rows.append(
                    f"      {_fmt_gib(copy.size):>10}  "
                    f"{(copy.resolution or '?'):<6} {copy.file}"
                )
        rows.extend(self._truncation_note(len(groups), len(shown)))
        return rows

    @staticmethod
    def _truncation_note(total: int, shown: int) -> List[str]:
        hidden = total - shown
        if hidden > 0:
            return [f"  ... and {hidden} more (see JSON report for the full list)"]
        return []
