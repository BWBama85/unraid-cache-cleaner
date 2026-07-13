"""CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .arr import ArrClientError, RadarrClient, SonarrClient
from .config import Config
from .extractor import Extractor, ExtractorError, summarize
from .planner import collapse_roots
from .plex import PlexClient, PlexClientError
from .plex_report import PlexDuplicateReporter
from .qbittorrent import QbittorrentClient, QbittorrentClientError
from .service import CleanerService
from .state import StateExtractionLedger, StateStore
from . import web
from .web_actions import ReclaimService
from .web_rescan import (
    ReportRescanService,
    report_generation_lock,
    report_generation_lock_path,
)


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level parser."""

    parser = argparse.ArgumentParser(
        prog="unraid-cache-cleaner",
        description="Safely delete qBittorrent leftovers from an Unraid cache mount.",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser("scan", help="Run one cleanup cycle and exit.")
    subparsers.add_parser("service", help="Poll qBittorrent forever.")
    subparsers.add_parser(
        "extract",
        help="Extract RAR archives in the download path so *arr can import them "
        "(opt-in via EXTRACT_ENABLED; honors DRY_RUN).",
    )
    subparsers.add_parser(
        "web",
        help="Serve the read-only Plex duplicate report as a web page + JSON API "
        "(reads the existing report; never scans or deletes).",
    )

    plex = subparsers.add_parser(
        "plex-duplicates",
        help="Report Plex library duplicates (read-only; never deletes).",
    )
    plex.add_argument(
        "--json-only",
        action="store_true",
        help="Write the JSON report only; suppress the printed table.",
    )
    plex.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap printed rows per section. Does not affect the JSON report.",
    )
    plex.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="ID",
        help="Library section ID to scan; repeatable; overrides PLEX_SECTIONS.",
    )

    return parser


def _safe_print(text: str) -> None:
    """Print, degrading gracefully when stdout cannot encode some characters.

    The rendered table interpolates raw Plex titles and file paths (e.g.
    ``Amélie``), so on a non-UTF-8 stdout (``PYTHONIOENCODING=ascii``, a C-locale
    console) a plain ``print`` would raise ``UnicodeEncodeError`` and turn a
    successful, already-written report into a traceback. Fall back to replacing
    only the characters the stream cannot represent.
    """

    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding))


def configure_logging(level: str) -> None:
    """Initialize process logging."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_cleaner(config: Config, command: str) -> int:
    """Run the qBittorrent cleanup ``scan`` / ``service`` commands."""

    logger = logging.getLogger(__name__)
    client = QbittorrentClient(
        config.qbittorrent_url,
        config.qbittorrent_username,
        config.qbittorrent_password,
        timeout_seconds=config.qbittorrent_timeout_seconds,
        verify_tls=config.qbittorrent_verify_tls,
        max_attempts=config.http_max_attempts,
    )
    state_store = StateStore(config.state_db_path)
    extractor = Extractor(config, ledger=StateExtractionLedger(state_store))
    service = CleanerService(config, client, state_store, extractor=extractor)

    if command == "scan":
        service.run_once()
        return 0

    # Opt-in: fold the web viewer into the service on a daemon thread (#34). A GET
    # only reads a file; the optional action layer (Phase 2) serializes its own
    # mutations. A bind failure must NOT take down the core cleanup loop — the web
    # UI is an optional convenience — so it is logged and skipped.
    if config.web_enabled:
        try:
            server = _build_web_server(config)
            server.start_background()
            _log_web_mode(config, logger)
            logger.info(
                "Plex duplicate report web UI listening on http://%s:%s (%s)",
                config.web_bind_address,
                server.port,
                "actions enabled" if config.web_actions_enabled else "read-only",
            )
        except (OSError, OverflowError, ValueError, sqlite3.Error) as exc:
            logger.error(
                "Web UI failed to start on %s:%s (%s); continuing cleanup without it",
                config.web_bind_address,
                config.web_port,
                exc,
            )

    service.serve_forever()
    return 0


def run_web(config: Config) -> int:
    """Serve the Plex duplicate report web UI (#34) — read-only by default, with the
    fail-closed action layer only when ``WEB_ENABLE_ACTIONS=true``."""

    logger = logging.getLogger(__name__)
    try:
        server = _build_web_server(config)
    except (OSError, OverflowError, ValueError, sqlite3.Error) as exc:
        # A bad bind address, an in-use/out-of-range port, or an unwritable audit
        # DB must fail with a clear message, not a raw traceback (fail-closed,
        # CLAUDE.md).
        logger.error(
            "Web UI failed to start on %s:%s: %s",
            config.web_bind_address,
            config.web_port,
            exc,
        )
        return 3
    _log_web_mode(config, logger)
    logger.info(
        "Plex duplicate report web UI listening on http://%s:%s (%s; serves %s)",
        config.web_bind_address,
        server.port,
        "actions enabled" if config.web_actions_enabled else "read-only",
        config.plex_duplicate_report_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down web UI")
    finally:
        server.shutdown()
    return 0


def _build_web_server(config: Config) -> "web.DuplicateReportServer":
    """Assemble the web server, wiring the action layer only when actions are on.

    The reclaim service owns the ``*arr`` clients and the audit store, so it is
    built here (where those constructors live) and injected into ``build_server``;
    when actions are disabled ``None`` is passed and the server is the read-only
    viewer."""

    provider = web.file_report_provider(config.plex_duplicate_report_path)
    reclaim_service = _build_reclaim_service(config, provider)
    rescan_service = _build_rescan_service(config)
    # Bind the socket BEFORE reconciling: a bad bind address, an in-use port, or an
    # unwritable audit DB must fail the start without the staging sweep having mutated
    # the media tree. The server is bound but not yet serving here, so the sweep still
    # runs before any request is accepted.
    server = web.build_server(
        config,
        provider=provider,
        reclaim_service=reclaim_service,
        rescan_service=rescan_service,
    )
    _reconcile_web_staging(reclaim_service, config)
    return server


def _build_rescan_service(config: Config) -> Optional[ReportRescanService]:
    """Build the report-regeneration service (#77), or ``None`` when it cannot run here.

    Requires the action layer (it is authorized by the same ``WEB_ACTION_TOKEN`` the
    reclaim path uses) *and* Plex credentials (a rescan fans out to Plex). When either is
    missing the rescan routes stay 404/405 and the "Regenerate report" button is hidden —
    a report generated elsewhere (cron/CLI) is still served read-only. The regeneration
    closure builds its Plex/``*arr`` clients per run, so none is created until a rescan
    actually runs."""

    if not config.web_actions_enabled:
        return None
    if not (config.plex_url and config.plex_token):
        return None
    lock_path = report_generation_lock_path(config.plex_duplicate_report_path)
    return ReportRescanService(
        lambda: _generate_and_publish(_build_reporter(config)), lock_path
    )


def _reconcile_web_staging(
    reclaim_service: Optional[ReclaimService], config: Config
) -> None:
    """Reconcile orphaned two-phase staging siblings (#72) at web startup, before the
    socket serves — restoring media a crash left staged and clearing completed-delete
    leftovers under the configured media roots.

    Best-effort: it needs the action layer (the reclaim service owns the media-path
    map and the audit store) and a path map to have roots to sweep, and any failure is
    logged and swallowed so reconciliation can never block the read-only viewer from
    starting."""

    logger = logging.getLogger(__name__)
    if reclaim_service is None or not config.web_media_path_map:
        return
    try:
        report = reclaim_service.reconcile_staging()
    except Exception:  # noqa: BLE001 — reconciliation must never block startup
        logger.warning("Startup staging reconciliation failed", exc_info=True)
        return
    if report.total:
        logger.info(
            "Startup staging reconciliation: restored=%s removed=%s would_remove=%s skipped=%s",
            report.restored,
            report.removed,
            report.would_remove,
            report.skipped,
        )


def _build_reclaim_service(
    config: Config, provider: "web.ReportProvider"
) -> Optional[ReclaimService]:
    """Build the fail-closed reclaim service, or ``None`` when actions are disabled.

    The audit store is a *separate* SQLite connection opened
    ``check_same_thread=False`` (WAL lets it coexist with the cleaner's connection)
    because reclaim audit rows are written from HTTP worker threads; the reclaim
    lock serializes those writes."""

    if not config.web_actions_enabled:
        return None
    audit_store = StateStore(config.state_db_path, check_same_thread=False)
    return ReclaimService(
        config,
        provider,
        radarr=_build_radarr(config),
        sonarr=_build_sonarr(config),
        audit=audit_store.record_actions,
        # The staging sweep (#74) reads the audit trail off the same store to tell a
        # committed-purge leftover (remove) from a crash-mid-move sibling (restore).
        audit_lookup=audit_store.recent_web_reclaim_actions,
    )


def _log_web_mode(config: Config, logger: logging.Logger) -> None:
    """Log a prominent warning when the destructive action layer is enabled."""

    if not config.web_actions_enabled:
        return
    logger.warning(
        "Web ACTION layer ENABLED (dry_run=%s): a reclaim deletes library media, routed "
        "by association (filesystem for untracked, Radarr/Sonarr for tracked) and gated "
        "by WEB_ACTION_TOKEN.",
        config.web_actions_dry_run,
    )
    if not config.web_action_token:
        logger.warning(
            "WEB_ENABLE_ACTIONS is set but WEB_ACTION_TOKEN is empty; every reclaim is "
            "refused until you set a token."
        )
    if not config.web_media_path_map:
        logger.info(
            "No WEB_MEDIA_PATH_MAP set: filesystem deletes of untracked copies are refused "
            "(tracked copies still reclaim via Radarr/Sonarr)."
        )


def _resolve_extract_roots(config: Config) -> Tuple[Path, ...]:
    """Resolve the download roots the ``extract`` command scans.

    Kept decoupled from qBittorrent/state on purpose: the one-shot extractor
    relies solely on ``WATCH_PATHS`` (fail-closed when unset) so it stays a
    standalone tool, mirroring how ``plex-duplicates`` avoids the cleaner stack.
    """

    if not config.watch_paths:
        raise ExtractorError(
            "The extract command needs WATCH_PATHS set to the mounted download path (e.g. /data)."
        )
    existing = tuple(
        root
        for root in collapse_roots(config.watch_paths)
        if root.exists() and root.is_dir()
    )
    if not existing:
        raise ExtractorError(
            "No configured WATCH_PATHS are mounted inside this container."
        )
    return existing


def run_extract(config: Config) -> int:
    """Run the one-shot RAR extraction pass."""

    logger = logging.getLogger(__name__)
    if not config.extract_enabled:
        logger.warning(
            "Extraction is disabled. Set EXTRACT_ENABLED=true to enable the extract command."
        )
        return 0

    roots = _resolve_extract_roots(config)
    # The extract command shares the cleaner's SQLite ledger (but not its
    # qBittorrent client): claim-before-extract keeps a one-shot `extract` from
    # double-extracting against a running `service`, and gives the standalone
    # command cross-run idempotency.
    state_store = StateStore(config.state_db_path)
    extractor = Extractor(config, ledger=StateExtractionLedger(state_store))
    results = extractor.extract_all(roots, dry_run=config.dry_run)

    counts = summarize(results)
    logger.info(
        "Extract complete: extracted=%s would_extract=%s deferred=%s skipped=%s failed=%s dry_run=%s",
        counts["extracted"],
        counts["would_extract"],
        counts["deferred_incomplete"],
        counts["skipped_present"],
        counts["failed"],
        config.dry_run,
    )
    for result in results:
        logger.info("%s: %s (%s)", result.status, result.archive, result.message)
    return 0


def _build_radarr(config: Config) -> Optional[RadarrClient]:
    """Construct a Radarr client only when both its URL and API key are set."""

    if config.radarr_url and config.radarr_api_key:
        return RadarrClient(
            config.radarr_url,
            config.radarr_api_key,
            timeout_seconds=config.radarr_timeout_seconds,
            verify_tls=config.radarr_verify_tls,
            max_attempts=config.http_max_attempts,
        )
    return None


def _build_sonarr(config: Config) -> Optional[SonarrClient]:
    """Construct a Sonarr client only when both its URL and API key are set."""

    if config.sonarr_url and config.sonarr_api_key:
        return SonarrClient(
            config.sonarr_url,
            config.sonarr_api_key,
            timeout_seconds=config.sonarr_timeout_seconds,
            verify_tls=config.sonarr_verify_tls,
            max_attempts=config.http_max_attempts,
        )
    return None


def _build_reporter(config: Config) -> PlexDuplicateReporter:
    """Construct the Plex duplicate reporter (Plex + optional ``*arr`` clients).

    Built lazily by both the ``plex-duplicates`` command and the web rescan job (#77),
    so no Plex client is created until a report is actually generated — preserving the
    credential-less read-only viewer deployment and the "a GET never reaches Plex"
    guarantee."""

    client = PlexClient(
        config.plex_url,
        config.plex_token,
        timeout_seconds=config.plex_timeout_seconds,
        verify_tls=config.plex_verify_tls,
        max_attempts=config.http_max_attempts,
    )
    return PlexDuplicateReporter(
        config,
        client,
        radarr_client=_build_radarr(config),
        sonarr_client=_build_sonarr(config),
    )


def _generate_and_publish(
    reporter: PlexDuplicateReporter, section_overrides=None
):
    """Generate the report, publish it atomically, and log the summary.

    The reporter's ``write_report`` is atomic (temp file + ``os.replace``), so a failure
    anywhere in ``generate`` leaves the previous report on disk untouched — the
    failure-retention guarantee the web rescan (#77) relies on."""

    report = reporter.generate(section_overrides=section_overrides)
    reporter.write_report(report)
    reporter.log_report(report)
    return report


def run_plex_duplicates(config: Config, args: argparse.Namespace) -> int:
    """Run the read-only Plex duplicate report.

    Holds the shared cross-process regeneration lock (#77) so a manual run and a
    concurrent web-triggered rescan never both fan out to Plex; if another regeneration
    already holds it, this run logs and skips (exit 0) rather than duplicating the work —
    the atomic writer would make a double-run harmless, but skipping avoids the wasted
    Plex/``*arr`` load."""

    logger = logging.getLogger(__name__)
    lock_path = report_generation_lock_path(config.plex_duplicate_report_path)
    with report_generation_lock(lock_path) as acquired:
        if not acquired:
            logger.info(
                "Another report regeneration is already in progress; skipping this run."
            )
            return 0
        reporter = _build_reporter(config)
        report = _generate_and_publish(reporter, section_overrides=args.section or None)
        if not args.json_only:
            _safe_print(reporter.render_table(report, limit=args.limit))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""

    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "service"

    config = Config.from_env()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    try:
        if command == "plex-duplicates":
            return run_plex_duplicates(config, args)
        if command == "extract":
            return run_extract(config)
        if command == "web":
            return run_web(config)
        return run_cleaner(config, command)
    except (QbittorrentClientError, PlexClientError, ArrClientError) as exc:
        logger.error(str(exc))
        return 2
    except RuntimeError as exc:
        logger.error(str(exc))
        return 3
    except KeyboardInterrupt:
        logger.info("Shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
