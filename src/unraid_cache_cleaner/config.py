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


def _parse_path_map(value: Optional[str]) -> tuple[tuple[Path, Path], ...]:
    """Parse ``WEB_MEDIA_PATH_MAP`` into ordered ``(plex_prefix, container_prefix)`` pairs.

    Syntax: comma-separated ``plex_prefix:container_prefix`` entries, e.g.
    ``/mnt/user/Media:/media,/mnt/user/TV:/tv``. Each entry is split on its FIRST
    ``:`` so a container path may itself contain none (Linux paths do not use
    ``:``; this is a Linux-container-only feature). An entry missing either side is
    skipped rather than raising — a partial map simply leaves the unmatched Plex
    paths unmapped, and an unmapped filesystem delete is *refused* (fail-closed),
    never guessed. Order is preserved so a caller can apply longest-prefix
    precedence deterministically.
    """

    if value is None or value.strip() == "":
        return ()
    pairs: list[tuple[Path, Path]] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        plex_prefix, container_prefix = entry.split(":", 1)
        plex_prefix = plex_prefix.strip()
        container_prefix = container_prefix.strip()
        if not plex_prefix or not container_prefix:
            continue
        pairs.append((Path(plex_prefix), Path(container_prefix)))
    return tuple(pairs)


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
    # Extracted output is protected from deletion for this window after it is
    # written, so freshly extracted media survives until Radarr/Sonarr import it —
    # even after the source torrent deregisters and its directory protection
    # vanishes. Once the window lapses, normal orphan rules resume so post-import
    # leftovers are eventually reclaimed. Default 24h, comfortably longer than the
    # orphan grace.
    extract_protect_seconds: int = 86400
    # Bounded retry-with-backoff shared by every read-oriented service HTTP client
    # (qBittorrent/Plex/Radarr/Sonarr) via the JsonHttpClient base. This is the
    # total number of attempts per idempotent (GET/HEAD) request: 1 = single
    # attempt / retries off (the historical default, so behavior is unchanged
    # until an operator opts in). Raise it (e.g. 3) so a transient 5xx or
    # connection blip on a LAN call is retried instead of failing the whole
    # read-only report on the first hiccup. A non-idempotent POST (qBittorrent's
    # login) is never retried. Appended last, with a default, so existing
    # positional Config(...) calls and from_env keep working.
    http_max_attempts: int = 1
    # Web GUI for the Plex duplicate report (#34, Phase 1: read-only viewer). The
    # `web` subcommand serves the on-disk report as a page + JSON API; it never
    # regenerates the report and exposes no delete/action path (Phase 2 follow-up).
    # ``web_enabled`` folds that viewer into the long-running ``service`` on a
    # background thread — default False, so ``service`` gains no listener until an
    # operator opts in. Binds to loopback by default (fail-closed); the Unraid
    # template sets 0.0.0.0 so a mapped container port is reachable. Appended with
    # defaults so existing positional Config(...) calls and from_env keep working.
    web_enabled: bool = False
    web_bind_address: str = "127.0.0.1"
    web_port: int = 8080
    # Web action layer (#34, Phase 2): the first *outside-triggered* mutation of
    # media in this project, so it is fail-closed on every axis. ``web_actions_enabled``
    # gates the reclaim endpoint entirely (default False → the server stays the
    # Phase 1 read-only viewer). ``web_actions_dry_run`` reports the would-delete
    # set and touches nothing (default True, mirroring DRY_RUN). ``web_action_token``
    # is a shared secret every reclaim request must present (via ``X-Action-Token``
    # or the form field); enabling actions WITHOUT a token is refused at request
    # time, so an unauthenticated mutation surface can never be exposed on 0.0.0.0.
    # ``web_media_path_map`` maps Plex-reported paths to this container's mounts so
    # a filesystem delete of an *untracked* copy can resolve to a real file — an
    # unmapped path is refused (the Plex media library is not mounted by default).
    # Appended with defaults so existing positional Config(...) calls and from_env
    # keep working.
    web_actions_enabled: bool = False
    web_actions_dry_run: bool = True
    web_action_token: str = ""
    web_media_path_map: tuple[tuple[Path, Path], ...] = ()
    # CSRF/origin hardening for a non-loopback bind (#63). When the server binds
    # beyond loopback (the Unraid ``0.0.0.0`` path), a browser reclaim form must
    # present a matching ``Origin`` (or same-origin ``Referer``); the JSON API stays
    # token-only when it sends no ``Origin`` (so ``curl`` still works). ``web_allowed_origins``
    # is an explicit allow-list of full origins (``scheme://host[:port]``) for a
    # deployment behind a TLS-terminating reverse proxy, where the server sees plain
    # HTTP and a client-supplied ``Host`` cannot be trusted to infer the external
    # scheme. Empty (the default) keeps the same-origin-vs-``Host`` check. Appended
    # with a default so existing positional Config(...) calls and from_env keep working.
    web_allowed_origins: tuple[str, ...] = ()
    # DNS-rebinding / Host-header hardening (#67). Every route trusts the client
    # ``Host`` header for nothing but this allow-list, applied *before* routing. An
    # IP-literal Host and ``localhost`` are always accepted (an IP cannot be
    # DNS-rebound, so direct LAN-by-IP access needs no config); a *hostname* Host must
    # appear here (or be derivable from ``web_allowed_origins``) or the request is
    # refused, which is the standard DNS-rebinding defense. Entries are bare hostnames
    # (an optional ``:port`` is ignored — Docker remaps the external port). Empty (the
    # default) still defends rebinding because only IP/loopback hosts pass. Appended
    # with a default so existing positional Config(...) calls and from_env keep working.
    web_allowed_hosts: tuple[str, ...] = ()
    # Lifetime (seconds) of the browser unlock session (#68/#79). Drives both the
    # cookie ``Max-Age`` and the signed-token expiry. A non-positive value falls back
    # to the built-in one-hour default (a zero/negative TTL would mint instantly-expired
    # credentials and break the two-step confirm). Appended with a default so existing
    # positional Config(...) calls and from_env keep working.
    web_action_session_seconds: int = 3600
    # Opt-in auth for the read-only reclaim *action history* views ``/actions`` +
    # ``/api/actions`` (#82). Default False keeps today's behavior: the history is
    # LAN-readable under the Host-allow-list model (#67), like ``/`` and ``/api/report``.
    # When True, those two routes require the same credential a reclaim does — the shared
    # ``WEB_ACTION_TOKEN`` (as an ``X-Action-Token`` header on ``/api/actions``) or a valid
    # unlock session (the ``ucc_session`` cookie a browser earns via the token->preview
    # flow). It gates *only* the audit history (which exposes previously-deleted absolute
    # paths + sizes); ``/`` and ``/api/report`` are unchanged. Fail-closed: with the gate
    # on but no ``WEB_ACTION_TOKEN`` set (or actions disabled), there is no credential to
    # accept, so the history is *denied*, never silently reopened — ``build_server`` warns
    # about the lockout at startup. Appended with a default so existing positional
    # Config(...) calls and from_env keep working.
    web_action_history_auth: bool = False
    # Opt-in auth for the *report* read surface ``/`` + ``/index.html`` + ``/api/report``
    # (#85). Default False keeps today's behavior: the report is LAN-readable under the
    # Host-allow-list model (#67), like the history views were before #82. When True, those
    # routes require the same credential a reclaim does — the shared ``WEB_ACTION_TOKEN`` (as
    # an ``X-Action-Token`` header on ``/api/report``) or a valid unlock session (the
    # ``ucc_session`` cookie). Independent of :attr:`web_action_history_auth`: enable *both*
    # to place the *entire* read surface behind the credential. Because ``/`` is the door a
    # browser unlocks through, gating it needs a no-JS unlock entry point — the 403 page
    # carries a bare token form posting to ``POST /actions/unlock`` (mints the session, then
    # 303-redirects back). Fail-closed like #82: with the gate on but no ``WEB_ACTION_TOKEN``
    # (or actions disabled) there is no credential to accept, so the report is *denied*, never
    # silently reopened — ``build_server`` warns about the lockout at startup. Appended with a
    # default so existing positional Config(...) calls and from_env keep working.
    web_action_report_auth: bool = False
    # Opt-in progressive-enhancement inline script for the reclaim form (#80). Default False
    # keeps the strict no-script CSP (``default-src 'none'``; scripts blocked entirely). When
    # True *and* actions are enabled, the report page carries a single self-contained inline
    # ``<script>`` (select-all + a live selected-count/size total) admitted by a per-response
    # ``script-src 'nonce-<random>'`` — no external asset, no fetch/XHR, and every path still
    # works with JavaScript disabled (the script is pure enhancement, never carries auth or
    # submits deletes). Appended with a default so existing positional Config(...) calls and
    # from_env keep working.
    web_action_inline_script: bool = False

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
            extract_protect_seconds=_parse_int(os.getenv("EXTRACT_PROTECT_SECONDS"), 86400),
            http_max_attempts=_parse_int(os.getenv("HTTP_MAX_ATTEMPTS"), 1),
            web_enabled=_parse_bool(os.getenv("WEB_ENABLED"), False),
            # A blank value (WEB_BIND_ADDRESS= in an env file) must fall back to
            # loopback, not "" — ThreadingHTTPServer(("", port)) binds all
            # interfaces, which would silently defeat the fail-closed default.
            web_bind_address=(os.getenv("WEB_BIND_ADDRESS") or "").strip() or "127.0.0.1",
            web_port=_parse_int(os.getenv("WEB_PORT"), 8080),
            web_actions_enabled=_parse_bool(os.getenv("WEB_ENABLE_ACTIONS"), False),
            web_actions_dry_run=_parse_bool(os.getenv("WEB_ACTIONS_DRY_RUN"), True),
            web_action_token=os.getenv("WEB_ACTION_TOKEN", ""),
            web_media_path_map=_parse_path_map(os.getenv("WEB_MEDIA_PATH_MAP")),
            web_allowed_origins=_parse_str_list(os.getenv("WEB_ALLOWED_ORIGINS")),
            web_allowed_hosts=_parse_str_list(os.getenv("WEB_ALLOWED_HOSTS")),
            web_action_session_seconds=_parse_int(os.getenv("WEB_ACTION_SESSION_SECONDS"), 3600),
            web_action_history_auth=_parse_bool(os.getenv("WEB_ACTION_HISTORY_AUTH"), False),
            web_action_report_auth=_parse_bool(os.getenv("WEB_ACTION_REPORT_AUTH"), False),
            web_action_inline_script=_parse_bool(os.getenv("WEB_ACTION_INLINE_SCRIPT"), False),
        )
        config.ensure_directories()
        return config

    def ensure_directories(self) -> None:
        """Create parent directories for persistent files."""

        self.state_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.plex_duplicate_report_path.parent.mkdir(parents=True, exist_ok=True)
