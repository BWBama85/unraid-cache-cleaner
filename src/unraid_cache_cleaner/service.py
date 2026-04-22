"""Scan orchestration and deletion logic."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from .config import Config
from .models import ActionRecord, CandidateRecord, RunReport, TorrentRecord
from .planner import build_protection_plan, collapse_roots, normalize_path, find_orphan_candidates, is_within_any
from .qbittorrent import QbittorrentClient
from .scanner import scan_filesystem
from .state import StateStore

LOGGER = logging.getLogger(__name__)


class CleanerService:
    """Runs a single scan or a polling loop."""

    def __init__(
        self,
        config: Config,
        client: QbittorrentClient,
        state_store: StateStore,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.state_store = state_store
        self.clock = clock
        self.sleeper = sleeper

    def infer_watch_roots(self, torrents: list[TorrentRecord]) -> tuple[Path, ...]:
        """Find roots to scan when WATCH_PATHS is not explicitly configured."""

        if self.config.watch_paths:
            return collapse_roots(self.config.watch_paths)

        inferred_roots = {self.client.fetch_default_save_path()}
        inferred_roots.update(torrent.save_path for torrent in torrents if str(torrent.save_path))
        return collapse_roots(tuple(inferred_roots))

    def _filter_existing_roots(self, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        existing = []
        for root in roots:
            normalized = normalize_path(root)
            if normalized.exists() and normalized.is_dir():
                existing.append(normalized)
                continue
            LOGGER.warning("Skipping watch root that is not mounted inside the container: %s", normalized)
        return tuple(existing)

    def run_once(self) -> RunReport:
        """Execute one scan cycle."""

        started_at = self.clock()
        warnings: list[str] = []
        errors: list[str] = []
        actions: list[ActionRecord] = []

        torrents = self.client.fetch_torrents()
        watch_roots = self._filter_existing_roots(self.infer_watch_roots(torrents))
        if not watch_roots:
            message = (
                "No valid watch roots found. Mount the download path into this container at the same "
                "internal path qBittorrent uses, or set WATCH_PATHS explicitly."
            )
            LOGGER.error(message)
            raise RuntimeError(message)

        protection_plan = build_protection_plan(
            torrents,
            watch_roots,
            protect_single_file_parent_dirs=self.config.protect_single_file_parent_dirs,
        )
        scanned_files = scan_filesystem(
            watch_roots,
            self.config.excluded_globs,
            protected_dirs=protection_plan.protected_dirs,
        )
        orphan_candidates = find_orphan_candidates(scanned_files, protection_plan)

        now = self.clock()
        self.state_store.sync_candidates(orphan_candidates, now)
        eligible = self.state_store.get_eligible_candidates(
            now,
            orphan_grace_seconds=self.config.orphan_grace_seconds,
            min_file_age_seconds=self.config.min_file_age_seconds,
        )

        if self.config.dry_run:
            actions.extend(
                ActionRecord(
                    path=candidate.path,
                    action="delete",
                    status="would_delete",
                    size=candidate.size,
                    message="dry-run mode",
                )
                for candidate in eligible
            )
        else:
            deleted_paths: list[Path] = []
            for candidate in eligible:
                action = self._delete_candidate(candidate)
                actions.append(action)
                if action.status in {"deleted", "missing"}:
                    deleted_paths.append(candidate.path)

            if deleted_paths:
                self.state_store.remove_candidates(deleted_paths)
                if self.config.delete_empty_dirs:
                    actions.extend(
                        self._remove_empty_dirs(
                            deleted_paths,
                            watch_roots,
                            protection_plan.protected_dirs,
                        )
                    )

        self.state_store.record_actions(actions, now)

        report = RunReport(
            started_at=started_at,
            finished_at=self.clock(),
            dry_run=self.config.dry_run,
            watch_roots=watch_roots,
            torrent_count=len(torrents),
            protected_dir_count=len(protection_plan.protected_dirs),
            tracked_file_count=len(protection_plan.tracked_files),
            scanned_file_count=len(scanned_files),
            orphan_candidate_count=len(orphan_candidates),
            eligible_count=len(eligible),
            actions=actions,
            warnings=warnings,
            errors=errors,
        )
        self.write_report(report)
        self.log_report(report)
        return report

    def _delete_candidate(self, candidate: CandidateRecord) -> ActionRecord:
        path = candidate.path
        if not path.exists():
            return ActionRecord(
                path=path,
                action="delete",
                status="missing",
                size=candidate.size,
                message="file already gone",
            )

        try:
            path.unlink()
        except OSError as exc:
            return ActionRecord(
                path=path,
                action="delete",
                status="failed",
                size=candidate.size,
                message=str(exc),
            )

        return ActionRecord(
            path=path,
            action="delete",
            status="deleted",
            size=candidate.size,
            message="deleted orphan file",
        )

    def _remove_empty_dirs(
        self,
        deleted_paths: list[Path],
        watch_roots: tuple[Path, ...],
        protected_dirs: tuple[Path, ...],
    ) -> list[ActionRecord]:
        roots = tuple(normalize_path(path) for path in watch_roots)
        protected = tuple(normalize_path(path) for path in protected_dirs)
        candidates: set[Path] = set()

        for file_path in deleted_paths:
            current = normalize_path(file_path.parent)
            while current not in roots and is_within_any(current, roots):
                if is_within_any(current, protected):
                    break
                candidates.add(current)
                current = current.parent

        removed: list[ActionRecord] = []
        for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue
            removed.append(
                ActionRecord(
                    path=directory,
                    action="rmdir",
                    status="deleted",
                    message="removed empty directory",
                )
            )
        return removed

    def write_report(self, report: RunReport) -> None:
        """Write the latest run report to disk."""

        payload = {
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "dry_run": report.dry_run,
            "watch_roots": [str(path) for path in report.watch_roots],
            "torrent_count": report.torrent_count,
            "protected_dir_count": report.protected_dir_count,
            "tracked_file_count": report.tracked_file_count,
            "scanned_file_count": report.scanned_file_count,
            "orphan_candidate_count": report.orphan_candidate_count,
            "eligible_count": report.eligible_count,
            "actions": [
                {
                    "path": str(action.path),
                    "action": action.action,
                    "status": action.status,
                    "size": action.size,
                    "message": action.message,
                }
                for action in report.actions
            ],
            "warnings": report.warnings,
            "errors": report.errors,
        }
        self.config.report_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def log_report(self, report: RunReport) -> None:
        """Emit a compact run summary."""

        LOGGER.info(
            "Run complete: torrents=%s scanned=%s candidates=%s eligible=%s actions=%s dry_run=%s",
            report.torrent_count,
            report.scanned_file_count,
            report.orphan_candidate_count,
            report.eligible_count,
            len(report.actions),
            report.dry_run,
        )
        for action in report.actions:
            LOGGER.info("%s %s: %s", action.status, action.action, action.path)

    def serve_forever(self) -> None:
        """Run continuously."""

        while True:
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("Scan cycle failed")
            self.sleeper(self.config.poll_interval_seconds)
