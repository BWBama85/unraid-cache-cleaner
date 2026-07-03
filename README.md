# Unraid Cache Cleaner

`unraid-cache-cleaner` is a small companion service for Unraid that watches the same download path qBittorrent writes to, polls the qBittorrent WebUI API, and removes leftover files only after they have been untracked long enough to be considered safe.

The old approach depended on copying a script into the qBittorrent container and doing one-off manual runs. This project is built to live outside that container and keep working as a normal Unraid service.

## Why This Version Is Safer

- It runs as its own container on Unraid.
- It authenticates to qBittorrent over the WebUI API instead of assuming localhost access inside the container.
- It protects every torrent that still exists in qBittorrent, not only actively downloading ones.
- It protects whole content directories for multi-file torrents, which keeps extracted files in active torrent folders from being misclassified.
- It tracks orphan candidates in SQLite and only deletes them after a grace window.
- It defaults to `DRY_RUN=true`.

## Operating Model

On each poll cycle the service:

1. Fetches the current torrent list from qBittorrent.
2. Infers which paths should be protected.
3. Scans the mounted cache/download roots.
4. Records untracked files as orphan candidates.
5. Deletes only candidates that have stayed untracked long enough.
6. Removes empty directories left behind by those deletions.

The safety rule is simple: content that still belongs to any torrent present in qBittorrent stays protected.

## Quick Start

### Unraid one-line install

On your Unraid server:

```bash
curl -fsSL https://raw.githubusercontent.com/BWBama85/unraid-cache-cleaner/main/scripts/install-unraid-template.sh | bash
```

That drops the Docker template into Unraid for you. Then go to the Docker tab, add the `unraid-cache-cleaner` container from the template, fill in your qBittorrent connection details, and leave `DRY_RUN=true` for the first start.

### 1. Mount the same internal path qBittorrent uses

If qBittorrent writes to `/data`, mount that exact same host path into this container at `/data` too. That removes most path mapping ambiguity.

### 2. Set the required environment variables

```bash
QBITTORRENT_URL=http://qbittorrent:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=change-me
DRY_RUN=true
WATCH_PATHS=/data
EXCLUDED_GLOBS=/data/logs/*,/data/orphaned-files/*,find_duplicates.sh,rar_extractor.sh,video_folders.log
```

A starter env file is included at [.env.example](.env.example).

### 3. Run a one-shot dry run

```bash
PYTHONPATH=src python3 -m unraid_cache_cleaner scan
```

### 4. Review the run report

The latest run summary is written to `/config/last-run.json` by default.

### 5. Turn on deletion

Set:

```bash
DRY_RUN=false
```

Then start the long-running service:

```bash
PYTHONPATH=src python3 -m unraid_cache_cleaner service
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` | qBittorrent WebUI base URL |
| `QBITTORRENT_USERNAME` | empty | WebUI username |
| `QBITTORRENT_PASSWORD` | empty | WebUI password |
| `QBITTORRENT_TIMEOUT_SECONDS` | `15` | HTTP timeout |
| `QBITTORRENT_VERIFY_TLS` | `true` | Verify TLS certificates |
| `WATCH_PATHS` | empty | Comma-separated mounted roots to scan |
| `POLL_INTERVAL_SECONDS` | `300` | Delay between scans in service mode |
| `ORPHAN_GRACE_SECONDS` | `21600` | How long a file must stay untracked before deletion |
| `MIN_FILE_AGE_SECONDS` | `1800` | Minimum file age before it can be deleted |
| `DRY_RUN` | `true` | Report only, no deletes |
| `DELETE_EMPTY_DIRS` | `true` | Remove empty directories after file deletion |
| `PROTECT_SINGLE_FILE_PARENT_DIRS` | `true` | Protect dedicated subdirs that contain tracked single-file torrents |
| `EXCLUDED_GLOBS` | _(adds to defaults)_ | Extra basename or full-path glob patterns to skip, merged with the built-in defaults |
| `STATE_DB_PATH` | `/config/state.sqlite3` | SQLite state database |
| `REPORT_PATH` | `/config/last-run.json` | JSON summary of the last run |
| `LOG_LEVEL` | `INFO` | Python log level |
| `PLEX_URL` | empty | Plex server base URL (e.g. `http://192.168.1.10:32400`) |
| `PLEX_TOKEN` | empty | Plex `X-Plex-Token` (sent as a header, never in the URL) |
| `PLEX_SECTIONS` | empty | Comma-separated library section **IDs** to scan; empty ⇒ auto-detect video sections |
| `PLEX_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Plex |
| `PLEX_VERIFY_TLS` | `true` | Verify TLS certificates (set `false` for a self-signed reverse proxy) |
| `PLEX_DUPLICATE_REPORT_PATH` | `/config/plex-duplicates.json` | JSON duplicate report output path |

> **Note:** the `PLEX_*` variables drive the [Plex Duplicate Report](#plex-duplicate-report) subcommand. They are unused by the `scan`/`service` cleanup commands — leave them empty if you only use qBittorrent cleanup.

If `WATCH_PATHS` is empty, the service falls back to qBittorrent's default save path plus any `save_path` values currently used by torrents. In practice, explicitly setting `WATCH_PATHS` is better on Unraid.

If your watch root also contains helper scripts, logs, or scratch folders that are not managed by qBittorrent, add them to `EXCLUDED_GLOBS`. Your patterns are merged with a built-in default list (`.DS_Store`, `*.part`, `*.!qB`, and other junk/temp patterns), so the defaults stay in effect and you only list your extras. Patterns without a slash match by basename. Patterns with a slash match against the full path inside the container.

Example:

```bash
EXCLUDED_GLOBS=/data/logs/*,/data/orphaned-files/*,find_duplicates.sh,rar_extractor.sh,video_folders.log
```

## Plex Duplicate Report

`plex-duplicates` is a **read-only** subcommand that asks Plex which library items
resolve to more than one file on disk, ranks each item's copies (resolution →
bitrate → size, so a smaller-but-better encode is never mistaken for the worse
copy), and reports how much space you could reclaim by keeping only the best
copy. **It never deletes, moves, or unmonitors anything** — it only writes a
report and prints a table.

```bash
PLEX_URL=http://192.168.1.10:32400 \
PLEX_TOKEN=your-x-plex-token \
PYTHONPATH=src python3 -m unraid_cache_cleaner plex-duplicates
```

With no `PLEX_SECTIONS` set it auto-detects your movie and TV libraries. It
writes a JSON report to `PLEX_DUPLICATE_REPORT_PATH` (default
`/config/plex-duplicates.json`) and prints a reclaimable-sorted table with three
sections:

- **Reclaimable (safe)** — duplicate/upgrade copies you can safely remove.
- **Review — possible mismatches (not counted)** — items Plex merged from
  *different* titles (e.g. the 1990 and 2014 *TMNT*). These are **never** counted
  as reclaimable; check them by hand.
- **⚠️ arr-tracked (Radarr/Sonarr)** — a placeholder, populated by a future
  release ([#8](https://github.com/BWBama85/unraid-cache-cleaner/issues/8)).

Flags:

| Flag | Purpose |
| --- | --- |
| `--json-only` | Write the JSON report only; suppress the printed table |
| `--limit N` | Cap printed rows per section (the JSON report is unaffected) |
| `--section ID` | Scan a specific library section ID; repeatable; overrides `PLEX_SECTIONS` |

Getting your token: open any item in Plex Web → **⋯ → Get Info → View XML**, and
copy the `X-Plex-Token` value from the URL. It travels as a request header, never
in a logged URL.

## Packaging

### Hosted Container

The repo publishes a container image automatically from GitHub Actions to:

```bash
ghcr.io/bwbama85/unraid-cache-cleaner:latest
```

### Local

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unraid_cache_cleaner scan
```

### Docker

```bash
docker build -t unraid-cache-cleaner .
docker run --rm \
  -e QBITTORRENT_URL=http://qbittorrent:8080 \
  -e QBITTORRENT_USERNAME=admin \
  -e QBITTORRENT_PASSWORD=change-me \
  -e WATCH_PATHS=/data \
  -e EXCLUDED_GLOBS='/data/logs/*,/data/orphaned-files/*,find_duplicates.sh,rar_extractor.sh,video_folders.log' \
  -e DRY_RUN=true \
  -v /mnt/user/appdata/unraid-cache-cleaner:/config \
  -v /mnt/cache/downloads:/data \
  unraid-cache-cleaner scan
```

The published image also supports:

```bash
docker run --rm ghcr.io/bwbama85/unraid-cache-cleaner:latest scan
```

### Unraid

See [docs/unraid.md](docs/unraid.md). A starter container template is included at [contrib/unraid-cache-cleaner.xml](contrib/unraid-cache-cleaner.xml), and the default repository path is the published GHCR image.

## Project Layout

- `src/unraid_cache_cleaner/`: runtime code
- `tests/`: unit tests
- `docs/`: architecture and deployment notes
- `contrib/unraid-cache-cleaner.xml`: starter Unraid template

## Important Behavior Notes

- This is intentionally conservative.
- Multi-file torrent content directories are protected as a unit.
- Single-file torrents located directly in the watch root only protect the tracked file itself.
- Single-file torrents in their own subdirectory protect that subdirectory when `PROTECT_SINGLE_FILE_PARENT_DIRS=true`.

That last rule is deliberate: it avoids deleting extracted content next to an active single-file torrent when the torrent lives in a dedicated folder, without blocking cleanup for flat watch roots.

## License

MIT
