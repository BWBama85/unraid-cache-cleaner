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
from .planner import is_within_any, normalize_path
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
SKIPPED_PRESENT = "skipped_present"
FAILED = "failed"

# ``ExtractionLedger.claim`` outcomes (the ledger protocol's contract; the SQLite
# implementation in ``state.py`` imports these).
CLAIM_NEW = "new"  # caller won the claim; proceed to extract
CLAIM_DONE = "done"  # already extracted; skip with no re-invoke
CLAIM_BUSY = "busy"  # a fresh claim is held elsewhere; skip this cycle


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


class ExtractionLedger(Protocol):
    """Claim/idempotency store injected into ``Extractor`` (fakeable in tests).

    Absent (``None``), the extractor re-processes every archive every run — the
    behavior of the foundation slice and of the pure unit tests. Present, it makes
    extraction idempotent across runs and claim-safe against a concurrent run.
    """

    def claim(self, archive: Path, now: float) -> str:
        """Return ``CLAIM_NEW`` / ``CLAIM_DONE`` / ``CLAIM_BUSY`` for ``archive``."""
        ...

    def complete(self, archive: Path, outputs: Sequence[Path], now: float) -> None:
        """Record a successful extraction and its output files."""
        ...

    def release(self, archive: Path) -> None:
        """Drop an in-flight claim so a deferred/failed archive retries."""
        ...


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of processing a single archive (or volume set)."""

    archive: Path
    status: str
    message: str = ""
    output_dir: Optional[Path] = None
    # Files this extraction created (empty unless a ledger recorded them). The
    # deletion planner protects these so extracted media survives until *arr
    # imports it (Child C).
    outputs: Tuple[Path, ...] = ()


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
        """Return True when the archive's payload verifies cleanly (a defer gate).

        Uses ``lsar -test``, which tests the integrity of the archived files —
        not plain ``lsar``, which only lists headers and would pass a settled but
        corrupt or missing-later-volume archive. A still-downloading, truncated,
        or corrupt archive exits non-zero, so it is deferred and retried rather
        than extracted. When no listing tool is available the check is skipped
        (returns True) and extraction becomes the real integrity gate.
        """

        if not self.list_tool or shutil.which(self.list_tool) is None:
            return True
        proc = self._runner(
            [self.list_tool, "-test", str(archive)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=self.timeout_seconds,
        )
        return proc.returncode == 0

    def extract(self, archive: Path, dest_dir: Path) -> None:
        """Extract ``archive`` into ``dest_dir`` (in place, overwriting).

        Mirrors the source tool's ``unrar x -y "$rarfile" "$dir/"``: extract the
        archive's contents directly into its own directory. ``-no-recursion``
        keeps it to the selected archive — ``unar`` otherwise auto-extracts
        archives nested *inside* it, which (with ``-force-overwrite``) would
        create/overwrite unexpected files. Raises ``ExtractorError`` on any
        non-zero exit.
        """

        cmd = [
            self.tool,
            "-quiet",
            "-no-directory",
            "-no-recursion",
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
        ledger: Optional[ExtractionLedger] = None,
        clock: Callable[[], float] = time.time,
        chown: Callable[[str, int, int], None] = os.chown,
    ) -> None:
        self.config = config
        self.tool: ArchiveTool = tool if tool is not None else UnarArchiveTool(config.extract_tool)
        self.ledger = ledger
        self.clock = clock
        self._chown = chown

    def find_first_volumes(self, roots: Tuple[Path, ...]) -> List[Tuple[Path, float]]:
        """Return ``(first_volume, group_newest_mtime)`` for each archive set.

        Reuses ``scan_filesystem`` so archive discovery inherits the exact
        symlink-skipping, excluded-glob, and normalization behavior the cleaner
        already relies on. Multi-volume sets collapse to their first volume; the
        newest mtime across the whole set — including legacy ``.rNN`` continuation
        volumes, which are not ``.rar`` and so not extracted directly — drives the
        settle guard so a set whose final part is still being written is not
        extracted early.
        """

        all_records = list(scan_filesystem(roots, self.config.excluded_globs))
        records_by_dir: dict[Path, List[FileRecord]] = {}
        for record in all_records:
            records_by_dir.setdefault(record.path.parent, []).append(record)

        groups: dict[Tuple[Path, str], List[Tuple[int, FileRecord]]] = {}
        selected: List[Tuple[Path, float]] = []
        for record in all_records:
            if record.path.suffix.lower() != ".rar":
                continue
            match = _PART_RE.match(record.path.name)
            if match:
                key = (record.path.parent, match.group("base").lower())
                groups.setdefault(key, []).append((int(match.group("num")), record))
            else:
                newest_mtime = self._newest_volume_mtime(record, records_by_dir)
                selected.append((normalize_path(record.path), newest_mtime))

        for members in groups.values():
            members.sort(key=lambda item: item[0])
            lowest_num = members[0][0]
            # RAR volume numbering starts at part01 (part00 appears rarely). A set
            # whose lowest present volume is > 1 is missing its first volume —
            # still downloading — so it is skipped until the first arrives.
            # Extracting a later volume alone would only fail the integrity test.
            if lowest_num > 1:
                continue
            first = members[0][1]
            newest_mtime = max(record.mtime for _, record in members)
            selected.append((normalize_path(first.path), newest_mtime))

        selected.sort(key=lambda item: str(item[0]))
        return selected

    @staticmethod
    def _newest_volume_mtime(
        rar_record: FileRecord,
        records_by_dir: "dict[Path, List[FileRecord]]",
    ) -> float:
        """Newest mtime across a legacy set: the ``.rar`` plus its ``.rNN`` volumes.

        Legacy multi-volume sets store continuation volumes as ``<base>.rNN``
        beside the ``.rar``. Those are typically written last while downloading,
        so the settle guard must see them even though only the ``.rar`` is
        extracted directly.
        """

        base = rar_record.path.stem  # movie.rar -> movie
        sibling_re = re.compile(r"^" + re.escape(base) + r"\.r\d+$", re.IGNORECASE)
        newest = rar_record.mtime
        for sibling in records_by_dir.get(rar_record.path.parent, ()):
            if sibling_re.match(sibling.path.name):
                newest = max(newest, sibling.mtime)
        return newest

    def extract_all(
        self,
        roots: Tuple[Path, ...],
        *,
        dry_run: bool,
        incomplete_roots: Sequence[Path] = (),
    ) -> List[ExtractionResult]:
        """Process every archive under ``roots``, isolating per-archive errors.

        ``incomplete_roots`` are content paths of torrents that have not finished
        downloading; any archive within one is deferred (the settle guard covers a
        fresh mtime, this covers a settled ``.rar`` whose torrent is still pulling
        other files).
        """

        if not self.tool.is_available():
            raise ExtractorError(f"extract tool not found: {self.config.extract_tool}")

        now = self.clock()
        incomplete = tuple(normalize_path(path) for path in incomplete_roots)
        results: List[ExtractionResult] = []
        for archive, newest_mtime in self.find_first_volumes(roots):
            try:
                if incomplete and is_within_any(archive, incomplete):
                    results.append(
                        ExtractionResult(
                            archive,
                            DEFERRED_INCOMPLETE,
                            "source torrent still downloading; deferred",
                        )
                    )
                    continue
                if (now - newest_mtime) < self.config.extract_min_age_seconds:
                    results.append(
                        ExtractionResult(
                            archive,
                            DEFERRED_INCOMPLETE,
                            "younger than EXTRACT_MIN_AGE_SECONDS; deferred",
                        )
                    )
                    continue
                results.append(self._extract_one(archive, dry_run=dry_run, now=now))
            except Exception as exc:  # noqa: BLE001 - one bad archive must not abort the run
                LOGGER.warning("Unexpected error processing %s: %s", archive, exc)
                results.append(ExtractionResult(archive, FAILED, str(exc)))
        return results

    def _extract_one(self, archive: Path, *, dry_run: bool, now: float) -> ExtractionResult:
        dest_dir = archive.parent

        # Dry-run is a read-only preview: it neither claims nor records, so it does
        # not consult the ledger (an already-extracted archive still reports
        # would_extract, matching the spec). A live run claims *before* the
        # integrity test so a concurrent run can't also grab the archive; the
        # claim is released on any defer/failure so the archive retries next cycle.
        claimed = False
        if self.ledger is not None and not dry_run:
            decision = self.ledger.claim(archive, now)
            if decision == CLAIM_DONE:
                return ExtractionResult(
                    archive, SKIPPED_PRESENT, "already extracted", output_dir=dest_dir
                )
            if decision == CLAIM_BUSY:
                return ExtractionResult(
                    archive, SKIPPED_PRESENT, "claimed by another run", output_dir=dest_dir
                )
            claimed = True

        try:
            integrity_ok = self.tool.test(archive)
        except Exception as exc:  # noqa: BLE001 - a failed test just defers the archive
            if claimed:
                self.ledger.release(archive)
            return ExtractionResult(
                archive, DEFERRED_INCOMPLETE, f"integrity test error: {exc}"
            )
        if not integrity_ok:
            if claimed:
                self.ledger.release(archive)
            return ExtractionResult(
                archive,
                DEFERRED_INCOMPLETE,
                "integrity test failed; archive may still be downloading",
            )

        if dry_run:
            return ExtractionResult(
                archive, WOULD_EXTRACT, "dry-run: integrity ok", output_dir=dest_dir
            )

        # Snapshot the destination *before* extracting so ownership and output
        # tracking apply to only the files this extraction creates (skipped when
        # neither a ledger nor an owner needs it, so the default path pays nothing).
        need_outputs = self.ledger is not None
        preexisting = (
            _snapshot_tree(dest_dir) if (self.config.extract_owner or need_outputs) else set()
        )
        try:
            self.tool.extract(archive, dest_dir)
        except Exception as exc:  # noqa: BLE001 - surface, keep the archive, retry next run
            if claimed:
                self.ledger.release(archive)
            return ExtractionResult(archive, FAILED, str(exc))

        new_files = _new_files_since(dest_dir, preexisting) if need_outputs else ()
        self._apply_ownership(dest_dir, preexisting)
        if self.ledger is not None:
            self.ledger.complete(archive, new_files, now)
        return ExtractionResult(
            archive, EXTRACTED, "extracted", output_dir=dest_dir, outputs=new_files
        )

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


def _new_files_since(root: Path, preexisting: set) -> Tuple[Path, ...]:
    """Return files created under ``root`` since the ``preexisting`` snapshot.

    Files only (never directories or symlinks) so the planner protects the
    concrete extracted media, not a directory that might be a watch root — the
    flat-watch-root case where protecting ``archive.parent`` would disable all
    cleanup.
    """

    created: List[Path] = []
    for current_root, _dirs, files in os.walk(root):
        for name in files:
            path = Path(current_root) / name
            if path in preexisting or path.is_symlink():
                continue
            created.append(path)
    return tuple(created)


def summarize(results: Sequence[ExtractionResult]) -> dict:
    """Count results by status for a compact one-line summary."""

    counts = {
        EXTRACTED: 0,
        WOULD_EXTRACT: 0,
        DEFERRED_INCOMPLETE: 0,
        SKIPPED_PRESENT: 0,
        FAILED: 0,
    }
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
