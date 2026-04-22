"""CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .config import Config
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

    return parser


def configure_logging(level: str) -> None:
    """Initialize process logging."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""

    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "service"

    config = Config.from_env()
    configure_logging(config.log_level)

    client = QbittorrentClient(
        config.qbittorrent_url,
        config.qbittorrent_username,
        config.qbittorrent_password,
        timeout_seconds=config.qbittorrent_timeout_seconds,
        verify_tls=config.qbittorrent_verify_tls,
    )
    state_store = StateStore(config.state_db_path)
    service = CleanerService(config, client, state_store)

    try:
        if command == "scan":
            service.run_once()
            return 0
        service.serve_forever()
        return 0
    except QbittorrentClientError as exc:
        logging.getLogger(__name__).error(str(exc))
        return 2
    except RuntimeError as exc:
        logging.getLogger(__name__).error(str(exc))
        return 3
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
