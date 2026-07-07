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


def _parse_str_list(value: Optional[str]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return ()
    parts = [item.strip() for item in value.split(",")]
    return tuple(part for part in parts if part)


def _parse_glob_list(value: Optional[str]) -> tuple[str, ...]:
    user_globs = []
    if value is not None:
        user_globs = [item.strip() for item in value.split(",") if item.strip()]
    # User-provided globs are added to the built-in defaults, not substituted for
    # them, so common junk (*.part, *.!qB, .DS_Store, ...) stays excluded even when
    # EXCLUDED_GLOBS is set. Order is preserved and duplicates are dropped.
    merged: list[str] = []
    for glob in (*DEFAULT_EXCLUDED_GLOBS, *user_globs):
        if glob not in merged:
            merged.append(glob)
    return tuple(merged)


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
    # Plex duplicate report (in progress, #4). Inert until the plex-duplicates
    # subcommand (#7) consumes them; appended with defaults so existing
    # Config(...) calls and from_env keep working.
    plex_url: str = ""
    plex_token: str = ""
    plex_sections: tuple[str, ...] = ()
    plex_timeout_seconds: int = 30
    plex_verify_tls: bool = True
    plex_duplicate_report_path: Path = Path("/config/plex-duplicates.json")
    # Optional Radarr/Sonarr association for the plex-duplicates report (#8).
    # Each is inert unless both its URL and API key are set; appended with
    # defaults so existing Config(...) calls and from_env keep working.
    radarr_url: str = ""
    radarr_api_key: str = ""
    radarr_timeout_seconds: int = 30
    radarr_verify_tls: bool = True
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    sonarr_timeout_seconds: int = 30
    sonarr_verify_tls: bool = True
    # RAR extraction (opt-in, #31 Child A). Extraction *mutates* the download
    # path, so it is off by default and honors DRY_RUN like deletion does. The
    # standalone `extract` subcommand consumes these today; folding extraction
    # into the scan/service cycle is a follow-up. Appended with defaults so
    # existing Config(...) calls and from_env keep working.
    extract_enabled: bool = False
    extract_tool: str = "unar"
    extract_owner: str = ""
    extract_min_age_seconds: int = 300

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
            plex_url=os.getenv("PLEX_URL", ""),
            plex_token=os.getenv("PLEX_TOKEN", ""),
            plex_sections=_parse_str_list(os.getenv("PLEX_SECTIONS")),
            plex_timeout_seconds=_parse_int(os.getenv("PLEX_TIMEOUT_SECONDS"), 30),
            plex_verify_tls=_parse_bool(os.getenv("PLEX_VERIFY_TLS"), True),
            plex_duplicate_report_path=Path(
                os.getenv("PLEX_DUPLICATE_REPORT_PATH", "/config/plex-duplicates.json")
            ),
            radarr_url=os.getenv("RADARR_URL", ""),
            radarr_api_key=os.getenv("RADARR_API_KEY", ""),
            radarr_timeout_seconds=_parse_int(os.getenv("RADARR_TIMEOUT_SECONDS"), 30),
            radarr_verify_tls=_parse_bool(os.getenv("RADARR_VERIFY_TLS"), True),
            sonarr_url=os.getenv("SONARR_URL", ""),
            sonarr_api_key=os.getenv("SONARR_API_KEY", ""),
            sonarr_timeout_seconds=_parse_int(os.getenv("SONARR_TIMEOUT_SECONDS"), 30),
            sonarr_verify_tls=_parse_bool(os.getenv("SONARR_VERIFY_TLS"), True),
            extract_enabled=_parse_bool(os.getenv("EXTRACT_ENABLED"), False),
            extract_tool=os.getenv("EXTRACT_TOOL", "unar"),
            extract_owner=os.getenv("EXTRACT_OWNER", ""),
            extract_min_age_seconds=_parse_int(os.getenv("EXTRACT_MIN_AGE_SECONDS"), 300),
        )
        config.ensure_directories()
        return config

    def ensure_directories(self) -> None:
        """Create parent directories for persistent files."""

        self.state_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.plex_duplicate_report_path.parent.mkdir(parents=True, exist_ok=True)
