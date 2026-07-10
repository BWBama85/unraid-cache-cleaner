"""CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
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

    # Opt-in: fold the read-only Plex duplicate viewer into the long-running
    # service so one container both cleans up and serves the report (#34). It runs
    # on a daemon thread that reads a file only — no shared qBittorrent client or
    # SQLite connection — so it needs no coordination with the poll loop and dies
    # with the process. Off by default, so `service` gains no listener unless asked.
    if config.web_enabled:
        server = web.build_server(config)
        server.start_background()
        logger.info(
            "Plex duplicate report viewer listening on http://%s:%s (read-only)",
            config.web_bind_address,
            server.port,
        )

    service.serve_forever()
    return 0


def run_web(config: Config) -> int:
    """Serve the read-only Plex duplicate report web viewer (#34)."""

    logger = logging.getLogger(__name__)
    server = web.build_server(config)
    logger.info(
        "Plex duplicate report viewer listening on http://%s:%s (read-only; serves %s)",
        config.web_bind_address,
        server.port,
        config.plex_duplicate_report_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down web viewer")
    finally:
        server.shutdown()
    return 0


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


def run_plex_duplicates(config: Config, args: argparse.Namespace) -> int:
    """Run the read-only Plex duplicate report."""

    client = PlexClient(
        config.plex_url,
        config.plex_token,
        timeout_seconds=config.plex_timeout_seconds,
        verify_tls=config.plex_verify_tls,
        max_attempts=config.http_max_attempts,
    )
    reporter = PlexDuplicateReporter(
        config,
        client,
        radarr_client=_build_radarr(config),
        sonarr_client=_build_sonarr(config),
    )
    report = reporter.generate(section_overrides=args.section or None)
    reporter.write_report(report)
    reporter.log_report(report)
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
