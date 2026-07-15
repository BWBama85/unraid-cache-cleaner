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

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from .config import Config
from .models import CLAIM_BUSY, CLAIM_DONE, CLAIM_NEW, ClaimResult, FileRecord
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

    def list_members(self, archive: Path) -> Optional[Sequence[Path]]:
        """Return the archive's file members, or ``None`` if unenumerable.

        Members are archive-relative paths (the caller maps them under the
        destination dir); directory entries are omitted. ``None`` — no listing
        tool, tool error, or unparseable output — tells the extractor to fall
        back to the ``(mtime, size)`` filesystem diff. Implementations are
        optional: :class:`Extractor` degrades to the diff for any tool lacking
        this method.
        """
        ...


class ExtractionLedger(Protocol):
    """Claim/idempotency store injected into ``Extractor`` (fakeable in tests).

    Absent (``None``), the extractor re-processes every archive every run — the
    behavior of the foundation slice and of the pure unit tests. Present, it makes
    extraction idempotent across runs and claim-safe against a concurrent run.

    ``claim`` takes the archive's on-disk ``size``/``mtime`` so the ledger can tell
    a genuinely new archive from a re-seen one at the same path, and returns a
    :class:`ClaimResult` carrying an ownership ``token`` the caller threads back
    into ``complete``/``release`` (#41).
    """

    def claim(self, archive: Path, now: float, *, size: int, mtime: float) -> ClaimResult:
        """Return the claim decision + ownership token for ``archive``."""
        ...

    def complete(
        self, archive: Path, outputs: Sequence[Path], now: float, *, token: Optional[str]
    ) -> None:
        """Record a successful extraction and its output files under ``token``."""
        ...

    def release(self, archive: Path, *, token: Optional[str]) -> None:
        """Drop our in-flight claim (matched by ``token``) so the archive retries."""
        ...


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of processing a single archive (or volume set)."""

    archive: Path
    status: str
    message: str = ""
    output_dir: Optional[Path] = None
    # Files this extraction created or overwrote — never an untouched sibling or
    # the source archive, and computed the same way with or without a ledger
    # (#105). The deletion planner protects these so extracted media survives
    # until *arr imports it (Child C). Empty for any result that never reached the
    # produced-set walk: dry-run, deferred, skipped, a failed extraction, or a
    # bookkeeping failure inside the walk.
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


def _safe_member_path(dest_dir: Path, member: Path) -> Optional[Path]:
    """Map an archive member to its normalized path under ``dest_dir``.

    Returns ``None`` for an absolute member or one containing a ``..`` component —
    a malicious or malformed archive must never resolve a produced-output path
    outside the destination dir. Normalization is lexical (``normalize_path``
    never dereferences symlinks), matching ``_finalize_output``'s guarantees; the
    result is only ever intersected with paths ``os.walk`` actually visited under
    ``dest_dir``, so it can neither invent a path nor escape the tree.

    Nested members need no special handling (#54). RAR archives spell a member path
    with a backslash; ``lsar`` reports it normalized to ``/``, and ``unar`` splits on
    it to recreate that same tree under ``dest_dir`` — so plain concatenation lands
    on exactly the extracted file, Linux and macOS alike. (``-no-directory`` only
    suppresses the archive-named *enclosing* dir; it does not flatten members.) The
    lone exception is a member holding a *literal* ``/``, which is not a path to
    ``unar`` but one name it sanitizes flat (``sub_deep.mkv`` on Linux,
    ``sub:deep.mkv`` on macOS). ``lsar`` normalizes both spellings identically, so
    that member is unmappable in principle and simply misses the walk, degrading to
    the ``(mtime, size)`` diff rather than guessing.
    """

    if member.is_absolute() or ".." in member.parts:
        return None
    return normalize_path(dest_dir / member)


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

    def list_members(self, archive: Path) -> Optional[Sequence[Path]]:
        """Enumerate the archive's file members via ``lsar -json``.

        Returns the file members as archive-relative paths (directory entries are
        dropped), or ``None`` when they cannot be enumerated — no ``lsar`` on
        PATH, a non-zero exit, an empty/garbled payload, or a subprocess error.
        ``None`` is a fail-closed signal: the extractor falls back to the
        ``(mtime, size)`` diff rather than mis-reporting an empty produced set.

        ``-no-recursion`` mirrors :meth:`extract`: ``lsar`` otherwise descends into
        an archive nested *inside* this one and lists that inner archive's members
        (names extraction never writes), while omitting the nested archive file it
        does write — so the listing must use the same non-recursion flag as the
        extraction to describe exactly what lands on disk.
        """

        if not self.list_tool or shutil.which(self.list_tool) is None:
            return None
        try:
            proc = self._runner(
                [self.list_tool, "-json", "-no-recursion", str(archive)],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        try:
            payload = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return None
        contents = payload.get("lsarContents") if isinstance(payload, dict) else None
        if not isinstance(contents, list):
            return None
        members: List[Path] = []
        for entry in contents:
            if not isinstance(entry, dict) or entry.get("XADIsDirectory"):
                continue
            name = entry.get("XADFileName")
            if isinstance(name, str) and name:
                members.append(Path(name))
        return members


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
                results.append(
                    self._extract_one(
                        archive, dry_run=dry_run, now=now, newest_mtime=newest_mtime
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad archive must not abort the run
                LOGGER.warning("Unexpected error processing %s: %s", archive, exc)
                results.append(ExtractionResult(archive, FAILED, str(exc)))
        return results

    def _extract_one(
        self, archive: Path, *, dry_run: bool, now: float, newest_mtime: float
    ) -> ExtractionResult:
        dest_dir = archive.parent

        # Dry-run is a read-only preview: it neither claims nor records, so it does
        # not consult the ledger (an already-extracted archive still reports
        # would_extract, matching the spec). A live run claims *before* the
        # integrity test so a concurrent run can't also grab the archive; the
        # claim is released on any defer/failure so the archive retries next cycle.
        # The claim carries the archive's (size, mtime) so a different archive later
        # written to this path re-extracts, and returns a token that authorizes our
        # later complete/release against a concurrent stale-claim reclaim (#41).
        claim_token: Optional[str] = None
        if self.ledger is not None and not dry_run:
            try:
                stat_result = archive.stat()
            except OSError as exc:
                return ExtractionResult(
                    archive, DEFERRED_INCOMPLETE, f"archive stat failed: {exc}"
                )
            # Identity = (first-volume size, the set's newest mtime). Using the set's
            # newest mtime — the same value the settle guard tracks — rather than the
            # first volume's own means a re-download that changes only a continuation
            # volume (``.part02.rar`` / legacy ``.rNN``) shifts the fingerprint and is
            # re-extracted, instead of being wrongly skipped as CLAIM_DONE.
            claim = self.ledger.claim(
                archive, now, size=stat_result.st_size, mtime=newest_mtime
            )
            if claim.decision == CLAIM_DONE:
                return ExtractionResult(
                    archive, SKIPPED_PRESENT, "already extracted", output_dir=dest_dir
                )
            if claim.decision == CLAIM_BUSY:
                return ExtractionResult(
                    archive, SKIPPED_PRESENT, "claimed by another run", output_dir=dest_dir
                )
            claim_token = claim.token

        try:
            integrity_ok = self.tool.test(archive)
        except Exception as exc:  # noqa: BLE001 - a failed test just defers the archive
            if claim_token is not None:
                self.ledger.release(archive, token=claim_token)
            return ExtractionResult(
                archive, DEFERRED_INCOMPLETE, f"integrity test error: {exc}"
            )
        if not integrity_ok:
            if claim_token is not None:
                self.ledger.release(archive, token=claim_token)
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
        # tracking apply only to the files this extraction creates or overwrites.
        # The snapshot is unconditional: it is what makes `outputs` mean "files this
        # extraction produced" for every caller, ledger or not. Skipping it for
        # ledger-less callers only emptied `before_files`, which made every walked
        # file — the untouched siblings and the source archive included — read as
        # produced (#105).
        before_files, before_dirs = _snapshot(dest_dir)
        try:
            self.tool.extract(archive, dest_dir)
        except Exception as exc:  # noqa: BLE001 - surface, keep the archive, retry next run
            if claim_token is not None:
                self.ledger.release(archive, token=claim_token)
            return ExtractionResult(archive, FAILED, str(exc))

        # The produced files are identified first (a cheap filesystem walk), then
        # persisted. If persistence fails (e.g. a concurrent run holds the ledger
        # DB lock), release the claim so the next cycle re-extracts and re-records —
        # but still surface the produced paths as ``outputs`` so this cycle protects
        # the media it just wrote rather than deleting it as an orphan.
        try:
            new_files = self._finalize_output(archive, dest_dir, before_files, before_dirs)
        except Exception as exc:  # noqa: BLE001 - keep the archive, retry next run
            if claim_token is not None:
                self.ledger.release(archive, token=claim_token)
            return ExtractionResult(
                archive, FAILED, f"post-extraction bookkeeping failed: {exc}"
            )

        if self.ledger is not None:
            # ``created_at`` is stamped now, at completion, so a slow extraction
            # does not age its output past the protection window before this cycle
            # can protect it.
            try:
                self.ledger.complete(archive, new_files, self.clock(), token=claim_token)
            except Exception as exc:  # noqa: BLE001 - keep the archive, retry next run
                if claim_token is not None:
                    self.ledger.release(archive, token=claim_token)
                return ExtractionResult(
                    archive,
                    FAILED,
                    f"post-extraction bookkeeping failed: {exc}",
                    output_dir=dest_dir,
                    outputs=new_files,
                )
        return ExtractionResult(
            archive, EXTRACTED, "extracted", output_dir=dest_dir, outputs=new_files
        )

    def _finalize_output(
        self,
        archive: Path,
        dest_dir: Path,
        before_files: "Dict[Path, Tuple[float, int]]",
        before_dirs: set,
    ) -> Tuple[Path, ...]:
        """One post-extraction walk: record the produced files and chown them.

        A file counts as *produced* when its path is new, when its ``(mtime, size)``
        fingerprint changed since the pre-extraction snapshot, **or** when the
        archive's own member list names it. ``unar`` overwrites in place, so an
        overwritten output (e.g. a partial file a failed prior attempt left behind,
        or an already-unpacked media file) usually differs from the copy it
        replaced and the fingerprint diff records it. The member list closes that
        heuristic's one blind spot (#43): a file the tool overwrites with
        byte-identical content *and* a restored identical mtime is indistinguishable
        from untouched by the diff alone, yet the archive still declares it, so it is
        still protected. When the tool cannot enumerate members the produced set is
        exactly the fingerprint diff (the prior behavior). Member names are matched
        only against paths ``os.walk`` actually visited under ``dest_dir``, so the
        member list can never invent a path, follow a symlink, or escape the tree.
        Only produced files (and newly created directories) are chowned — never
        untouched siblings, which for a loose archive at a watch root would be the
        whole mount — and symlinks are never followed. A chown miss is warned once
        and never aborts output collection: freshly extracted media must be
        protected even when the process lacks ``CAP_CHOWN``.
        """

        expected_members = self._expected_member_paths(archive, dest_dir)
        owner = self._resolve_owner()
        produced: List[Path] = []
        chown_ok = owner is not None
        for current_root, dirs, files in os.walk(dest_dir):
            for name in files:
                path = Path(current_root) / name
                if path.is_symlink():
                    continue
                try:
                    stat_result = path.stat()
                except OSError:
                    continue
                fingerprint = (stat_result.st_mtime, stat_result.st_size)
                prior = before_files.get(path)
                unchanged = prior is not None and prior == fingerprint
                if unchanged and path not in expected_members:
                    continue  # untouched pre-existing file (e.g. the source archive)
                produced.append(path)
                if chown_ok:
                    chown_ok = self._chown_entry(path, owner, dest_dir)
            for name in dirs:
                path = Path(current_root) / name
                if not chown_ok or path.is_symlink() or path in before_dirs:
                    continue
                chown_ok = self._chown_entry(path, owner, dest_dir)
        return tuple(produced)

    def _expected_member_paths(self, archive: Path, dest_dir: Path) -> "frozenset[Path]":
        """The archive's members mapped to normalized paths under ``dest_dir``.

        An empty set is the fail-closed fallback: it is returned whenever the tool
        lacks ``list_members``, cannot enumerate the archive (returns ``None``), or
        raises — in which case ``_finalize_output`` degrades to the pure
        ``(mtime, size)`` diff. Members that would resolve outside ``dest_dir``
        (absolute or ``..``) are dropped by :func:`_safe_member_path`.
        """

        lister = getattr(self.tool, "list_members", None)
        if lister is None:
            return frozenset()
        try:
            members = lister(archive)
        except Exception as exc:  # noqa: BLE001 - fall back to the fingerprint diff
            LOGGER.debug("member enumeration failed for %s: %s", archive, exc)
            return frozenset()
        if not members:
            return frozenset()
        mapped = (_safe_member_path(dest_dir, Path(member)) for member in members)
        return frozenset(path for path in mapped if path is not None)

    def _resolve_owner(self) -> Optional[Tuple[int, int]]:
        if not self.config.extract_owner:
            return None
        try:
            return _parse_owner(self.config.extract_owner)
        except ValueError:
            LOGGER.warning(
                "Invalid EXTRACT_OWNER %r (expected numeric uid:gid); skipping chown",
                self.config.extract_owner,
            )
            return None

    def _chown_entry(self, path: Path, owner: Tuple[int, int], dest_dir: Path) -> bool:
        """chown ``path``; return whether chown should keep going this run."""

        try:
            self._chown(str(path), owner[0], owner[1])
            return True
        except OSError as exc:
            LOGGER.warning("Failed to change ownership under %s: %s", dest_dir, exc)
            return False


def _snapshot(root: Path) -> "Tuple[Dict[Path, Tuple[float, int]], set]":
    """Map pre-extraction files to a ``(mtime, size)`` fingerprint, plus the set of
    pre-existing dirs.

    Lets the post-extraction walk tell the files an extraction produced (new path,
    or a changed mtime/size from an overwrite) from untouched siblings. No symlink
    traversal (``os.walk`` never follows links by default).
    """

    files: "Dict[Path, Tuple[float, int]]" = {}
    dirs: set = set()
    for current_root, subdirs, filenames in os.walk(root):
        for name in subdirs:
            dirs.add(Path(current_root) / name)
        for name in filenames:
            path = Path(current_root) / name
            try:
                stat_result = path.stat()
            except OSError:
                continue
            files[path] = (stat_result.st_mtime, stat_result.st_size)
    return files, dirs


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
