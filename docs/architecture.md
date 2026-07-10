# Architecture

## Goal

Delete leftover cache/download files that qBittorrent failed to remove, without needing to copy code into the qBittorrent container and without deleting files that still belong to a live torrent.

## Core Design Choices

### Container-first, not plugin-first

This project is designed as an Unraid companion container. That keeps it decoupled from the qBittorrent image, easier to update, and easier to deploy through a Community Applications template.

### qBittorrent is the source of truth

The service polls the qBittorrent WebUI API. If a torrent still exists in qBittorrent, its content remains protected.

### Directory protection beats file matching for active content

The old script matched individual files. That is fragile for RAR and extraction workflows because extracted content may never appear in qBittorrent's tracked file list.

This implementation protects:

- full content directories for multi-file torrents
- the exact file for single-file torrents
- optionally the parent directory for single-file torrents when the torrent lives in its own subdirectory

### Time-based deletion

A file is not deleted the first time it appears orphaned. It must:

1. be outside all protected paths
2. remain untracked for `ORPHAN_GRACE_SECONDS`
3. be older than `MIN_FILE_AGE_SECONDS`

That gives qBittorrent, unpackers, and external movers time to settle before the file is touched.

## Main Components

### `http_client.py`

The shared `urllib` JSON-over-HTTP base (`JsonHttpClient`) that `qbittorrent.py`, `plex.py`, and `arr.py` subclass. It owns opener construction (the fail-closed `HostBoundRedirectHandler` from `http_redirect.py` + the TLS-verify toggle + a User-Agent), the request/read/JSON-decode path, and the `HTTPError`/`URLError`/`OSError` → `*ClientError` taxonomy. A subclass sets `service_name` + `error_class`, supplies its credential via `_auth_headers()` (never in a URL), and overrides an `_on_*_error` hook only where it needs a status-specific message. This is where a cross-cutting fix (redirect-guard policy, connect-timeout tuning, a token-in-logs guarantee) lands once for all three clients.

### `qbittorrent.py`

Minimal authenticated client (subclass of `JsonHttpClient`) for:

- `POST /api/v2/auth/login`
- `GET /api/v2/app/defaultSavePath`
- `GET /api/v2/torrents/info`

Adds a session `CookieJar` (via `_extra_handlers`) and keeps its own `_request` for the form-encoded login POST and one-shot 403 re-authentication.

### `planner.py`

Builds the protection plan:

- exact tracked files
- protected directories
- root collapsing and path normalization

### `scanner.py`

Recursively scans watch roots while skipping protected subtrees and excluded system/temp files.

### `state.py`

Stores the live orphan candidate set in SQLite so the service can apply a grace window across polling cycles.

### `service.py`

Coordinates one scan cycle or an endless polling loop, applies deletion policy, removes empty directories, and writes the latest report.

## Plex duplicate reporting

A separate, **read-only** capability that reports reclaimable disk space from duplicate media. It never deletes. Run it with `unraid-cache-cleaner plex-duplicates`.

### `plex.py`

Minimal token-authenticated Plex Web API client — a `JsonHttpClient` subclass with an `X-Plex-Token` auth header. Fetches library sections and the raw duplicate `Metadata` for a section, paging via `X-Plex-Container-Start/Size`. Also holds `build_duplicate_group`, the pure parser that turns one raw `Metadata` item (`Media → Part`, plus `Guid` external ids) into a `DuplicateGroup`, collapsing a `Media`'s parts under a shared `media_id`.

### `dedupe.py`

Pure, dependency-free analysis over the Plex models — no I/O. It:

- ranks the copies in a group best-first by `(resolution_rank, bitrate, size)`, so a smaller-but-better encode (e.g. 1080p x265 under a 720p copy) is still kept;
- merges stacked parts (copies sharing a Plex `media_id`) into a single logical copy, summing their sizes, so a split-file title is not mistaken for a duplicate — while `rank_copies_with_parts`/`rank_physical_copies` expose the underlying physical part files so the report can show each part's true path and size without changing the analysis math;
- classifies each group as `identical`, `upgrade`, or `mismatch` — where `mismatch` means the copies' file paths carry different `{imdb-…}`/`{tmdb-…}` ids (Plex merged different titles);
- computes reclaimable bytes under the hard safety rule that **mismatch groups are never counted as reclaimable**, and summarizes totals per section (`kind`) and overall.

### `plex_report.py`

The read-only orchestrator (`PlexDuplicateReporter`), mirroring `service.py`'s `run_once → write_report → log_report` shape. It resolves the video sections to scan (explicit `--section`/`PLEX_SECTIONS` ids, or auto-detected movie/TV libraries), fetches and parses each section's duplicates, analyzes them with `dedupe`, then writes a stable `sort_keys` JSON report and renders a reclaimable-sorted table. The renderer is pure (`DuplicateReport → str`), and the reporter takes an injectable `clock` so the JSON is byte-identical across runs on the same input. Stacked multi-part copies are represented faithfully: each JSON copy carries a `parts` array of its physical files and true per-file sizes, and a `mismatch` group's `copies` are the physical parts (unmerged) so the review view surfaces each conflicting file rather than one stack-merged row. The subcommand is wired in `cli.py` (`plex-duplicates`), which builds a `PlexClient` without touching the qBittorrent client or state DB.

### `arr.py` (optional)

An optional Radarr/Sonarr association layer that enriches the report with whether each copy is `*arr`-tracked — so a redundant copy Radarr/Sonarr tracks is flagged as "delete via the `*arr` or it re-downloads", while an untracked copy is confirmed safe. Two thin clients (`RadarrClient`, `SonarrClient`) subclass the shared `JsonHttpClient` base — custom `ArrClientError`, TLS-verify toggle, timeouts, `X-Api-Key` header (never in a URL). `annotate` is a pure transform over analyzed `DuplicateGroup`s. Each tracked file's `*arr` id is captured in the same fetch and attached per physical part (`MediaCopy.arr_file_id`), so the report can serialize it and the action layer can reclaim by id (#61) instead of re-resolving live at delete time; an id that can't be pinned unambiguously stays `None`.

The join differs by kind, because the two id joins are not equally reliable. **Movies** are *id-anchored*: Plex's `tmdb://` guid is the same TMDB id Radarr keys on, so within a group whose id Radarr tracks, the basename-matching copy is `tracked` and the other redundant copies are `untracked` (safe); if the id is absent, not in Radarr, or no basename matches, every copy is `unknown`. **Episodes** match by *basename only*: Plex's episode `Guid`s are episode-level, not the series TVDB id Sonarr keys on, so a copy whose basename Sonarr tracks is `tracked` and any other copy is `unknown` — never falsely `untracked`. The reporter constructs a client only when both its URL and API key are set; an unconfigured layer leaves the report byte-identical to a Plex-only run, and a configured-but-unreachable `*arr` logs a warning and degrades that kind to `unknown` rather than failing the report.

### `web.py` (viewer + HTTP wiring)

The project's **first inbound listener** — every other surface is an outbound
client. A stdlib `ThreadingHTTPServer` + `BaseHTTPRequestHandler` (no
Flask/FastAPI, per the stdlib-only rule) that serves the on-disk
`plex_duplicate_report_path` snapshot as a browsable HTML page (`/`) and a JSON
API (`/api/report`), plus a `/healthz` liveness route and a read-only
action-history view (`/actions` + `/api/actions`). The GET path is rigorously
read-only: it constructs **no** Plex/`*arr`/qBittorrent client and never
regenerates the report — a page load only reads a file the `plex-duplicates`
subcommand (or a cron) already wrote, so it can never fan out to Plex. The one GET
that touches SQLite is the history view, a bounded, indexed, newest-first SELECT of
the `web-reclaim:*` audit rows over a long-lived, query-only connection (opened once,
reused, never creating or migrating the DB) so a page load is a pure SELECT that never
checkpoints (`state.WebActionHistoryReader`). The
rendering functions are pure over the report dict, and the report is supplied by
an injectable provider (the default reads the file; tests inject a fake), so the
server is unit-tested end-to-end on an ephemeral port without any network
service. Safety envelope: every Plex-supplied string is HTML-escaped, routes are
explicit (no directory serving/CORS/external assets) under a strict
`Content-Security-Policy`, and a missing/truncated/malformed report degrades to an
empty-state page rather than a `500`. When no action layer is attached (the
default), every non-`GET` verb is `405`. Run it standalone with the `web`
subcommand, or fold it into the `service` loop on a daemon thread with
`WEB_ENABLED=true`. To keep concurrent reads safe, `write_report` publishes the
report atomically (temp file + `os.replace`), so the viewer never observes a
half-written file.

### `web_actions.py` (action layer, opt-in)

The **first outside-triggered mutation of media**, off unless
`WEB_ENABLE_ACTIONS=true`. `web.py` parses the reclaim request (`POST /api/reclaim`
JSON, or the no-JS `POST /actions/reclaim` browser form) into
`{rating_key, part_id}` targets and hands them to `ReclaimService`, which owns the
entire safety envelope and is constructed with injected collaborators (report
provider, filesystem deleter, Radarr/Sonarr clients, audit sink, clock) so every
path is fake-tested without a real socket or disk write. Fail-closed on every
axis: disabled + dry-run by default; a shared `WEB_ACTION_TOKEN` is required
(enabling actions without one refuses every request); on top of the token a
CSRF/origin check scales with the bind address — loopback stays permissive (the
default), but a non-loopback bind requires a browser reclaim *form* to present a
matching `Origin`/`Referer` (a cross-site form POST is refused even without
`Origin`), while the JSON API stays token-only when no `Origin` is sent, and a
`WEB_ALLOWED_ORIGINS` allow-list covers a TLS-terminating reverse proxy; targets
are resolved only against a *fresh* server-side report snapshot
(client-supplied path/association/size/backend are never trusted) and a stale
`generated_at` is a `409`; the keeper, `mismatch` groups, and `unknown`
associations are refused; one lock serializes snapshot → validate → delete →
audit. Routing is by association — an `untracked` copy is a filesystem delete
(only when `WEB_MEDIA_PATH_MAP` resolves the Plex path to a *mounted* container
file, re-`lstat`'d for a matching size and a regular non-symlink file under the
media root — TOCTOU), a `tracked` copy is a Radarr/Sonarr `DELETE` by the `*arr`
file id the report records for it (#61), re-validated by a single by-id `GET`
immediately before the delete — refused on a 404 or a current-basename mismatch
(`*arr`-side drift), and refused with a regenerate hint if the report predates the
serialized id (no full-library fan-out). A stacked copy's parts are all
prevalidated before any is deleted, and every real delete (or partial failure) is
persisted via `ActionRecord` in the SQLite `actions` table.

## Failure Model

The service fails closed:

- missing credentials stops the run
- missing watch roots stop the run
- files are only removed in non-dry-run mode
- failed deletes are logged and kept in state for later retries

## Current Scope

This version focuses on safe orphan cleanup for qBittorrent download/cache paths. It ships a web viewer for the Plex duplicate report (`web.py`) and an opt-in, fail-closed action layer to reclaim duplicates from the browser (`web_actions.py`, `WEB_ENABLE_ACTIONS=true`), but does not yet:

- subscribe to qBittorrent push events
- regenerate the report or trigger a Plex rescan from the web UI (it serves the on-disk snapshot)
- offer an undo/quarantine for a completed delete (a read-only action-history UI exists at `/actions`, backed by the audited `web-reclaim:*` rows, but a delete is not reversible)
- publish metrics

### Opt-in RAR extraction

Module `extractor.py` detects RAR archives under the watch roots, integrity-tests
them via the free `unar`/`lsar` binaries, and extracts them in place so
Radarr/Sonarr can import — the first-party replacement for the external
`rar_extractor.sh` cron. It is **off by default** (`EXTRACT_ENABLED=false`),
honors `DRY_RUN`, and reuses `scan_filesystem` for archive discovery so it
inherits the same symlink-skipping and excluded-glob behavior as the cleaner. The
archive tool is injected via the constructor, so tests pass a fake and
`unrar`/`p7zip` can be adapted later.

Extraction is folded into `CleanerService.run_once`, running **before** the
orphan-deletion pass. Each successful extraction's output files are persisted in
an `extraction_outputs` SQLite table and injected into that cycle's
`ProtectionPlan` as first-party tracked files, so freshly extracted media is
never deletable in the run that created it — or in any run for
`EXTRACT_PROTECT_SECONDS` afterward, which covers the single-file `.rar`,
loose-archive-at-the-watch-root, and torrent-deregistration cases the [directory
protection](#directory-protection-beats-file-matching-for-active-content) model
alone does not. Protection is by **exact file path**, not by directory, so an
archive extracted at the watch root does not protect (and disable cleanup of) the
whole mount. An `extractions` table records claims and completions: a
claim-before-extract guard stops a one-shot `extract` from colliding with a
running `service`, and completed archives are skipped on later cycles. The
standalone `extract` subcommand runs the same engine against `WATCH_PATHS` alone.

