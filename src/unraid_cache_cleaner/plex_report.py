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
import time
from typing import Callable, List, Optional, Sequence, Tuple

from . import arr, dedupe
from .arr import ArrClientError, RadarrClient, SonarrClient
from .config import Config
from .models import DuplicateGroup, DuplicateReport, MediaCopy, PlexSection
from .plex import PlexClient, build_duplicate_group

LOGGER = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: Plex library ``type`` -> (duplicate-query item ``type``, ``DuplicateGroup.kind``).
#: ``1`` = movie, ``4`` = episode. Only these video library types are scanned; a
#: music/photo section (or an unknown one) is never treated as a video library.
#: Kept as one mapping so the item-type and kind can't drift out of lockstep.
_SECTION_SPEC = {"movie": (1, "movie"), "show": (4, "episode")}


def _fmt_gib(num_bytes: int) -> str:
    return f"{num_bytes / _GIB:.1f} GiB"


def _copy_json(copy: MediaCopy, *, include_arr: bool = False) -> dict:
    payload = {
        "file": str(copy.file),
        "size": copy.size,
        "resolution": copy.resolution,
        "bitrate": copy.bitrate,
    }
    # Only serialized when the arr layer ran, so a Plex-only report stays
    # byte-identical to the pre-#8 shape.
    if include_arr:
        payload["association"] = copy.association
        payload["arr_tracked"] = copy.arr_tracked
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
        analyzed.sort(key=self._group_sort_key)

        if self._arr_enabled:
            radarr_index, sonarr_basenames = self._build_arr_indexes(warnings)
            analyzed = arr.annotate(analyzed, radarr_index, sonarr_basenames)

        summary = dedupe.summarize(analyzed)

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
        )

    def _build_arr_indexes(self, warnings: List[str]):
        """Fetch the Radarr/Sonarr tracked indexes, degrading gracefully.

        An unconfigured client contributes an empty index; a configured but
        unreachable one logs a warning and also contributes an empty index — so
        an ``*arr`` outage never fails the read-only report, it just leaves that
        kind ``unknown``.
        """

        radarr_index: dict = {}
        sonarr_basenames: set = set()
        if self.radarr_client is not None:
            try:
                radarr_index = self.radarr_client.fetch_tracked_index()
            except ArrClientError as exc:
                warnings.append(f"Radarr association skipped: {exc}")
        if self.sonarr_client is not None:
            try:
                sonarr_basenames = self.sonarr_client.fetch_tracked_index()
            except ArrClientError as exc:
                warnings.append(f"Sonarr association skipped: {exc}")
        return radarr_index, sonarr_basenames

    def _summary(self, report: DuplicateReport):
        """The report's precomputed summary, recomputed only if absent."""

        if report.summary is not None:
            return report.summary
        return dedupe.summarize(report.groups)

    @staticmethod
    def _group_sort_key(group: DuplicateGroup) -> Tuple[int, str, str]:
        # reclaimable desc, then a stable tiebreak so two runs on the same input
        # serialize byte-identically.
        return (-group.reclaimable_bytes, group.kind, group.rating_key)

    def _group_json(self, group: DuplicateGroup, *, include_arr: bool = False) -> dict:
        keeper = group.keeper
        return {
            "rating_key": group.rating_key,
            "title": group.title,
            "kind": group.kind,
            "classification": group.classification,
            "reclaimable_bytes": group.reclaimable_bytes,
            "keeper": _copy_json(keeper, include_arr=include_arr) if keeper is not None else None,
            "copies": [
                _copy_json(copy, include_arr=include_arr)
                for copy in dedupe.rank_copies(group)
            ],
        }

    def build_payload(self, report: DuplicateReport) -> dict:
        """Return the stable JSON payload for ``report`` (also used by tests)."""

        summary = self._summary(report)
        include_arr = report.arr_enabled
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
                self._group_json(group, include_arr=include_arr) for group in report.groups
            ],
            "warnings": report.warnings,
            "errors": report.errors,
        }
        # Added only when the arr layer ran, so a Plex-only report is byte-identical
        # to the pre-#8 shape.
        if include_arr:
            payload["arr_enabled"] = True
            payload["totals"]["arr_tracked_reclaimable_count"] = self._arr_reclaimable_tracked_count(
                report
            )
        return payload

    @staticmethod
    def _reclaim_candidates(group: DuplicateGroup) -> List[MediaCopy]:
        """The copies a reclaim would delete: every logical copy but the keeper."""

        return dedupe.rank_copies(group)[1:]

    def _arr_reclaimable_tracked_count(self, report: DuplicateReport) -> int:
        """Count reclaim-candidate copies an *arr tracks (delete ⇒ re-download)."""

        count = 0
        for group in report.groups:
            if group.classification == dedupe.MISMATCH:
                continue
            count += sum(
                1 for copy in self._reclaim_candidates(group) if copy.association == arr.TRACKED
            )
        return count

    def write_report(self, report: DuplicateReport) -> None:
        """Write the duplicate report as stable, ``sort_keys`` JSON."""

        payload = self.build_payload(report)
        self.config.plex_duplicate_report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True)
        )

    def log_report(self, report: DuplicateReport) -> None:
        """Emit one compact summary line mirroring ``service.log_report``.

        Skip warnings (e.g. a bad ``--section`` id) are logged first so they
        surface even when the scan finds no duplicates and returns early.
        """

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
        LOGGER.info(
            "Plex duplicates: sections=%s groups=%s reclaimable=%s mismatches=%s%s",
            len(report.sections),
            summary.group_count,
            _fmt_gib(summary.reclaimable_bytes),
            summary.mismatch_count,
            arr_note,
        )

    def render_table(
        self, report: DuplicateReport, *, limit: Optional[int] = None
    ) -> str:
        """Render the human-readable, reclaimable-sorted table (pure)."""

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

        # Every non-mismatch group is listed, including any whose reclaimable
        # bytes are 0 (e.g. copies Plex reports without a size) — so the section
        # rows always account for every group the header counts.
        reclaimable = [
            group for group in report.groups if group.classification != dedupe.MISMATCH
        ]
        reclaimable.sort(key=self._group_sort_key)
        total = sum(group.reclaimable_bytes for group in reclaimable)
        lines.append(
            f"Reclaimable (safe) - {_fmt_gib(total)} across {len(reclaimable)} groups"
        )
        lines.extend(
            self._render_reclaimable_rows(reclaimable, limit, arr_enabled=report.arr_enabled)
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
    ) -> List[str]:
        if not groups:
            return ["  (none)"]
        shown = groups if limit is None else groups[:limit]
        rows: List[str] = []
        for group in shown:
            keeper_res = (group.keeper.resolution or "?") if group.keeper else "?"
            copies = len(dedupe.rank_copies(group))
            tag = self._group_arr_tag(group) if arr_enabled else ""
            rows.append(
                f"  {_fmt_gib(group.reclaimable_bytes):>10}  "
                f"{group.classification:<9} {group.kind:<7} "
                f"keep={keeper_res:<5} copies={copies}  {group.title}{tag}"
            )
        rows.extend(self._truncation_note(len(groups), len(shown)))
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

        flagged = [
            (group, tracked)
            for group in reclaimable
            for tracked in [
                [c for c in self._reclaim_candidates(group) if c.association == arr.TRACKED]
            ]
            if tracked
        ]
        if not flagged:
            return ["  (no reclaimable copy is *arr-tracked - all safe to delete)"]

        shown = flagged if limit is None else flagged[:limit]
        rows: List[str] = []
        for group, tracked in shown:
            service = tracked[0].arr_tracked or "*arr"
            rows.append(f"  {group.kind:<7} {group.title}  (tracked by {service})")
            for copy in tracked:
                rows.append(
                    f"      {_fmt_gib(copy.size):>10}  "
                    f"{(copy.resolution or '?'):<6} {copy.file}"
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
            for copy in dedupe.rank_copies(group):
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
