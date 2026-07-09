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

### `qbittorrent.py`

Minimal authenticated client for:

- `POST /api/v2/auth/login`
- `GET /api/v2/app/defaultSavePath`
- `GET /api/v2/torrents/info`

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

Minimal token-authenticated Plex Web API client (same `urllib` + `*ClientError` pattern as `qbittorrent.py`). Fetches library sections and the raw duplicate `Metadata` for a section, paging via `X-Plex-Container-Start/Size`. Also holds `build_duplicate_group`, the pure parser that turns one raw `Metadata` item (`Media → Part`, plus `Guid` external ids) into a `DuplicateGroup`, collapsing a `Media`'s parts under a shared `media_id`.

### `dedupe.py`

Pure, dependency-free analysis over the Plex models — no I/O. It:

- ranks the copies in a group best-first by `(resolution_rank, bitrate, size)`, so a smaller-but-better encode (e.g. 1080p x265 under a 720p copy) is still kept;
- merges stacked parts (copies sharing a Plex `media_id`) into a single logical copy, summing their sizes, so a split-file title is not mistaken for a duplicate — while `rank_copies_with_parts`/`rank_physical_copies` expose the underlying physical part files so the report can show each part's true path and size without changing the analysis math;
- classifies each group as `identical`, `upgrade`, or `mismatch` — where `mismatch` means the copies' file paths carry different `{imdb-…}`/`{tmdb-…}` ids (Plex merged different titles);
- computes reclaimable bytes under the hard safety rule that **mismatch groups are never counted as reclaimable**, and summarizes totals per section (`kind`) and overall.

### `plex_report.py`

The read-only orchestrator (`PlexDuplicateReporter`), mirroring `service.py`'s `run_once → write_report → log_report` shape. It resolves the video sections to scan (explicit `--section`/`PLEX_SECTIONS` ids, or auto-detected movie/TV libraries), fetches and parses each section's duplicates, analyzes them with `dedupe`, then writes a stable `sort_keys` JSON report and renders a reclaimable-sorted table. The renderer is pure (`DuplicateReport → str`), and the reporter takes an injectable `clock` so the JSON is byte-identical across runs on the same input. Stacked multi-part copies are represented faithfully: each JSON copy carries a `parts` array of its physical files and true per-file sizes, and a `mismatch` group's `copies` are the physical parts (unmerged) so the review view surfaces each conflicting file rather than one stack-merged row. The subcommand is wired in `cli.py` (`plex-duplicates`), which builds a `PlexClient` without touching the qBittorrent client or state DB.

### `arr.py` (optional)

An optional Radarr/Sonarr association layer that enriches the report with whether each copy is `*arr`-tracked — so a redundant copy Radarr/Sonarr tracks is flagged as "delete via the `*arr` or it re-downloads", while an untracked copy is confirmed safe. Two thin `urllib` clients (`RadarrClient`, `SonarrClient`) reuse the `qbittorrent.py`/`plex.py` pattern — custom `ArrClientError`, TLS-verify toggle, timeouts, `X-Api-Key` header (never in a URL). `annotate` is a pure transform over analyzed `DuplicateGroup`s.

The join differs by kind, because the two id joins are not equally reliable. **Movies** are *id-anchored*: Plex's `tmdb://` guid is the same TMDB id Radarr keys on, so within a group whose id Radarr tracks, the basename-matching copy is `tracked` and the other redundant copies are `untracked` (safe); if the id is absent, not in Radarr, or no basename matches, every copy is `unknown`. **Episodes** match by *basename only*: Plex's episode `Guid`s are episode-level, not the series TVDB id Sonarr keys on, so a copy whose basename Sonarr tracks is `tracked` and any other copy is `unknown` — never falsely `untracked`. The reporter constructs a client only when both its URL and API key are set; an unconfigured layer leaves the report byte-identical to a Plex-only run, and a configured-but-unreachable `*arr` logs a warning and degrades that kind to `unknown` rather than failing the report.

## Failure Model

The service fails closed:

- missing credentials stops the run
- missing watch roots stop the run
- files are only removed in non-dry-run mode
- failed deletes are logged and kept in state for later retries

## Current Scope

This version focuses on safe orphan cleanup for qBittorrent download/cache paths. It does not yet:

- subscribe to qBittorrent push events
- expose a web UI
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

