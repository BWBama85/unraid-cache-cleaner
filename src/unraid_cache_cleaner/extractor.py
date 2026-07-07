"""RAR archive detection and extraction.

Ports the standalone ``rar_extractor`` bash tool (#31) into a first-party,
opt-in capability. This module is the *foundation* slice (Child A): a small,
injectable ``Extractor`` plus the ``extract`` one-shot subcommand's engine. It
deliberately does **not** touch the scan/service deletion cycle, ``RunReport``,
``StateStore``, or the planner — those are follow-up children.

The archive tool is shelled out to an external OS binary (the Python stdlib has
no RAR support and third-party libraries are disallowed). The default is the
free ``unar``/``lsar`` pair (Debian ``main``); the invocation is injected via the
constructor so tests pass a fake and ``unrar``/``p7zip`` can be adapted later.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

from .config import Config
from .models import FileRecord
from .planner import normalize_path
from .scanner import scan_filesystem

LOGGER = logging.getLogger(__name__)

# Modern multi-volume RAR sets: ``name.part01.rar``, ``name.part02.rar``, ...
# Only the lowest-numbered volume is handed to the tool; ``unar`` auto-detects
# the rest. Legacy ``name.rar`` + ``name.r00`` sets need no special handling here
# because the continuation volumes are not ``*.rar`` and the scan skips them.
_PART_RE = re.compile(r"^(?P<base>.+)\.part(?P<num>\d+)\.rar$", re.IGNORECASE)

# Extraction outcomes. Kept as bare strings (not an enum) to match the string
# ``status`` fields already used across the codebase (``ActionRecord.status``).
EXTRACTED = "extracted"
WOULD_EXTRACT = "would_extract"
DEFERRED_INCOMPLETE = "deferred_incomplete"
FAILED = "failed"


class ExtractorError(RuntimeError):
    """Raised when archive extraction cannot proceed safely."""


class ArchiveTool(Protocol):
    """The archive-tool contract injected into ``Extractor`` (fakeable in tests)."""

    def is_available(self) -> bool:
        ...

    def test(self, archive: Path) -> bool:
        ...

    def extract(self, archive: Path, dest_dir: Path) -> None:
        ...


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of processing a single archive (or volume set)."""

    archive: Path
    status: str
    message: str = ""
    output_dir: Optional[Path] = None


def _derive_list_tool(tool: str) -> Optional[str]:
    """Derive the sibling ``lsar`` listing tool from an ``unar`` binary path.

    ``unar`` extracts but cannot list; ``lsar`` (shipped in the same Debian
    package) does the read-only integrity/listing check. Returns ``None`` when a
    non-``unar`` binary is configured, in which case extraction itself becomes
    the only integrity gate.
    """

    path = Path(tool)
    name = path.name
    if name.endswith("unar"):
        return str(path.with_name(name[: -len("unar")] + "lsar"))
    return None


def _parse_owner(owner: str) -> Tuple[int, int]:
    """Parse a numeric ``uid:gid`` owner string (e.g. Unraid's ``99:100``)."""

    parts = owner.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected uid:gid, got {owner!r}")
    return int(parts[0]), int(parts[1])


class UnarArchiveTool:
    """Tests and extracts archives via the external ``unar``/``lsar`` binaries."""

    def __init__(
        self,
        tool: str = "unar",
        *,
        list_tool: Optional[str] = None,
        timeout_seconds: int = 3600,
        runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    ) -> None:
        self.tool = tool
        self.list_tool = list_tool if list_tool is not None else _derive_list_tool(tool)
        self.timeout_seconds = timeout_seconds
        self._runner = runner

    def is_available(self) -> bool:
        return shutil.which(self.tool) is not None

    def test(self, archive: Path) -> bool:
        """Return True when the archive reads cleanly (a cheap pre-extract gate).

        A still-downloading or truncated archive makes ``lsar`` exit non-zero, so
        it is deferred and retried rather than extracted. When no listing tool is
        available the check is skipped (returns True) and extraction becomes the
        real integrity gate.
        """

        if not self.list_tool or shutil.which(self.list_tool) is None:
            return True
        proc = self._runner(
            [self.list_tool, str(archive)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=self.timeout_seconds,
        )
        return proc.returncode == 0

    def extract(self, archive: Path, dest_dir: Path) -> None:
        """Extract ``archive`` into ``dest_dir`` (in place, overwriting).

        Mirrors the source tool's ``unrar x -y "$rarfile" "$dir/"``: extract the
        archive's contents directly into its own directory. Raises
        ``ExtractorError`` on any non-zero exit.
        """

        cmd = [
            self.tool,
            "-quiet",
            "-no-directory",
            "-force-overwrite",
            "-output-directory",
            str(dest_dir),
            str(archive),
        ]
        proc = self._runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else "no output"
            raise ExtractorError(
                f"{self.tool} exited {proc.returncode} for {archive.name}: {tail}"
            )


class Extractor:
    """Finds RAR archives under the watch roots and extracts them in place."""

    def __init__(
        self,
        config: Config,
        *,
        tool: Optional[ArchiveTool] = None,
        clock: Callable[[], float] = time.time,
        chown: Callable[[str, int, int], None] = os.chown,
    ) -> None:
        self.config = config
        self.tool: ArchiveTool = tool if tool is not None else UnarArchiveTool(config.extract_tool)
        self.clock = clock
        self._chown = chown

    def find_first_volumes(self, roots: Tuple[Path, ...]) -> List[Tuple[Path, float]]:
        """Return ``(first_volume, group_newest_mtime)`` for each archive set.

        Reuses ``scan_filesystem`` so archive discovery inherits the exact
        symlink-skipping, excluded-glob, and normalization behavior the cleaner
        already relies on. Multi-volume sets collapse to their first volume; the
        newest mtime across the whole set drives the settle guard so a set whose
        final part is still being written is not extracted early.
        """

        records = [
            record
            for record in scan_filesystem(roots, self.config.excluded_globs)
            if record.path.suffix.lower() == ".rar"
        ]

        groups: dict[Tuple[Path, str], List[Tuple[int, FileRecord]]] = {}
        selected: List[Tuple[Path, float]] = []
        for record in records:
            match = _PART_RE.match(record.path.name)
            if match:
                key = (record.path.parent, match.group("base").lower())
                groups.setdefault(key, []).append((int(match.group("num")), record))
            else:
                selected.append((normalize_path(record.path), record.mtime))

        for members in groups.values():
            members.sort(key=lambda item: item[0])
            first = members[0][1]
            newest_mtime = max(record.mtime for _, record in members)
            selected.append((normalize_path(first.path), newest_mtime))

        selected.sort(key=lambda item: str(item[0]))
        return selected

    def extract_all(
        self,
        roots: Tuple[Path, ...],
        *,
        dry_run: bool,
    ) -> List[ExtractionResult]:
        """Process every archive under ``roots``, isolating per-archive errors."""

        if not self.tool.is_available():
            raise ExtractorError(f"extract tool not found: {self.config.extract_tool}")

        now = self.clock()
        results: List[ExtractionResult] = []
        for archive, newest_mtime in self.find_first_volumes(roots):
            try:
                if (now - newest_mtime) < self.config.extract_min_age_seconds:
                    results.append(
                        ExtractionResult(
                            archive,
                            DEFERRED_INCOMPLETE,
                            "younger than EXTRACT_MIN_AGE_SECONDS; deferred",
                        )
                    )
                    continue
                results.append(self._extract_one(archive, dry_run=dry_run))
            except Exception as exc:  # noqa: BLE001 - one bad archive must not abort the run
                LOGGER.warning("Unexpected error processing %s: %s", archive, exc)
                results.append(ExtractionResult(archive, FAILED, str(exc)))
        return results

    def _extract_one(self, archive: Path, *, dry_run: bool) -> ExtractionResult:
        dest_dir = archive.parent

        try:
            integrity_ok = self.tool.test(archive)
        except Exception as exc:  # noqa: BLE001 - a failed test just defers the archive
            return ExtractionResult(
                archive, DEFERRED_INCOMPLETE, f"integrity test error: {exc}"
            )
        if not integrity_ok:
            return ExtractionResult(
                archive,
                DEFERRED_INCOMPLETE,
                "integrity test failed; archive may still be downloading",
            )

        if dry_run:
            return ExtractionResult(
                archive, WOULD_EXTRACT, "dry-run: integrity ok", output_dir=dest_dir
            )

        # Snapshot the destination *before* extracting so ownership is applied to
        # only the files this extraction creates (skipped entirely when no owner
        # is configured, so the default off case pays nothing).
        preexisting = _snapshot_tree(dest_dir) if self.config.extract_owner else set()
        try:
            self.tool.extract(archive, dest_dir)
        except Exception as exc:  # noqa: BLE001 - surface, keep the archive, retry next run
            return ExtractionResult(archive, FAILED, str(exc))

        self._apply_ownership(dest_dir, preexisting)
        return ExtractionResult(archive, EXTRACTED, "extracted", output_dir=dest_dir)

    def _apply_ownership(self, dest_dir: Path, preexisting: set) -> None:
        """Best-effort chown of the files this extraction created.

        Scoped to newly-created, non-symlink entries: chowning ``archive.parent``
        wholesale would rewrite ownership of unrelated siblings — the entire mount
        when a loose archive sits at a watch root — and following a symlink would
        chown an out-of-tree target, breaking the scanner's never-follow-symlinks
        invariant. Failure is a warning, never fatal: on many hosts the process
        lacks ``CAP_CHOWN``, and a permissions miss must not turn a successful
        extraction into an error.
        """

        if not self.config.extract_owner:
            return
        try:
            uid, gid = _parse_owner(self.config.extract_owner)
        except ValueError:
            LOGGER.warning(
                "Invalid EXTRACT_OWNER %r (expected numeric uid:gid); skipping chown",
                self.config.extract_owner,
            )
            return

        try:
            for current_root, dirs, files in os.walk(dest_dir):
                for name in dirs + files:
                    path = Path(current_root) / name
                    if path in preexisting or path.is_symlink():
                        continue
                    self._chown(str(path), uid, gid)
        except OSError as exc:
            LOGGER.warning("Failed to change ownership under %s: %s", dest_dir, exc)


def _snapshot_tree(root: Path) -> set:
    """Return every path under ``root`` (no symlink traversal), for chown scoping."""

    seen: set = set()
    for current_root, dirs, files in os.walk(root):
        for name in dirs + files:
            seen.add(Path(current_root) / name)
    return seen


def summarize(results: Sequence[ExtractionResult]) -> dict:
    """Count results by status for a compact one-line summary."""

    counts = {EXTRACTED: 0, WOULD_EXTRACT: 0, DEFERRED_INCOMPLETE: 0, FAILED: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
