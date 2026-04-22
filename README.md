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
| `EXCLUDED_GLOBS` | system/temp patterns | Basename or full-path glob patterns to skip |
| `STATE_DB_PATH` | `/config/state.sqlite3` | SQLite state database |
| `REPORT_PATH` | `/config/last-run.json` | JSON summary of the last run |
| `LOG_LEVEL` | `INFO` | Python log level |

If `WATCH_PATHS` is empty, the service falls back to qBittorrent's default save path plus any `save_path` values currently used by torrents. In practice, explicitly setting `WATCH_PATHS` is better on Unraid.

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
  -e DRY_RUN=true \
  -v /mnt/user/appdata/unraid-cache-cleaner:/config \
  -v /mnt/cache/downloads:/data \
  unraid-cache-cleaner scan
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
