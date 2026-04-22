"""Configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_EXCLUDED_GLOBS = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".~lock.*",
    "*.part",
    "*.!qB",
    "._*",
)


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _parse_int(value: Optional[str], default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _parse_path_list(value: Optional[str]) -> tuple[Path, ...]:
    if value is None or value.strip() == "":
        return ()
    parts = [item.strip() for item in value.split(",")]
    return tuple(Path(part) for part in parts if part)


def _parse_glob_list(value: Optional[str]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return DEFAULT_EXCLUDED_GLOBS
    parts = [item.strip() for item in value.split(",")]
    return tuple(item for item in parts if item)


@dataclass(frozen=True)
class Config:
    """Runtime configuration."""

    qbittorrent_url: str
    qbittorrent_username: str
    qbittorrent_password: str
    qbittorrent_timeout_seconds: int
    qbittorrent_verify_tls: bool
    watch_paths: tuple[Path, ...]
    poll_interval_seconds: int
    orphan_grace_seconds: int
    min_file_age_seconds: int
    dry_run: bool
    delete_empty_dirs: bool
    protect_single_file_parent_dirs: bool
    excluded_globs: tuple[str, ...]
    state_db_path: Path
    report_path: Path
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        """Build config from environment variables."""

        config = cls(
            qbittorrent_url=os.getenv("QBITTORRENT_URL", "http://qbittorrent:8080"),
            qbittorrent_username=os.getenv("QBITTORRENT_USERNAME", ""),
            qbittorrent_password=os.getenv("QBITTORRENT_PASSWORD", ""),
            qbittorrent_timeout_seconds=_parse_int(os.getenv("QBITTORRENT_TIMEOUT_SECONDS"), 15),
            qbittorrent_verify_tls=_parse_bool(os.getenv("QBITTORRENT_VERIFY_TLS"), True),
            watch_paths=_parse_path_list(os.getenv("WATCH_PATHS")),
            poll_interval_seconds=_parse_int(os.getenv("POLL_INTERVAL_SECONDS"), 300),
            orphan_grace_seconds=_parse_int(os.getenv("ORPHAN_GRACE_SECONDS"), 21600),
            min_file_age_seconds=_parse_int(os.getenv("MIN_FILE_AGE_SECONDS"), 1800),
            dry_run=_parse_bool(os.getenv("DRY_RUN"), True),
            delete_empty_dirs=_parse_bool(os.getenv("DELETE_EMPTY_DIRS"), True),
            protect_single_file_parent_dirs=_parse_bool(
                os.getenv("PROTECT_SINGLE_FILE_PARENT_DIRS"),
                True,
            ),
            excluded_globs=_parse_glob_list(os.getenv("EXCLUDED_GLOBS")),
            state_db_path=Path(os.getenv("STATE_DB_PATH", "/config/state.sqlite3")),
            report_path=Path(os.getenv("REPORT_PATH", "/config/last-run.json")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        config.ensure_directories()
        return config

    def ensure_directories(self) -> None:
        """Create parent directories for persistent files."""

        self.state_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
