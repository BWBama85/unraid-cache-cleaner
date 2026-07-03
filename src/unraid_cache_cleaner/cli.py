"""CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .config import Config
from .plex import PlexClient, PlexClientError
from .plex_report import PlexDuplicateReporter
from .qbittorrent import QbittorrentClient, QbittorrentClientError
from .service import CleanerService
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level parser."""

    parser = argparse.ArgumentParser(
        prog="unraid-cache-cleaner",
        description="Safely delete qBittorrent leftovers from an Unraid cache mount.",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser("scan", help="Run one cleanup cycle and exit.")
    subparsers.add_parser("service", help="Poll qBittorrent forever.")

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

    client = QbittorrentClient(
        config.qbittorrent_url,
        config.qbittorrent_username,
        config.qbittorrent_password,
        timeout_seconds=config.qbittorrent_timeout_seconds,
        verify_tls=config.qbittorrent_verify_tls,
    )
    state_store = StateStore(config.state_db_path)
    service = CleanerService(config, client, state_store)

    if command == "scan":
        service.run_once()
        return 0
    service.serve_forever()
    return 0


def run_plex_duplicates(config: Config, args: argparse.Namespace) -> int:
    """Run the read-only Plex duplicate report."""

    client = PlexClient(
        config.plex_url,
        config.plex_token,
        timeout_seconds=config.plex_timeout_seconds,
        verify_tls=config.plex_verify_tls,
    )
    reporter = PlexDuplicateReporter(config, client)
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
        return run_cleaner(config, command)
    except (QbittorrentClientError, PlexClientError) as exc:
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
