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
| `EXTRACT_ENABLED` | `false` | Enable [RAR extraction](#rar-extraction) in the `scan`/`service` cycle and the `extract` subcommand (opt-in; extraction mutates) |
| `EXTRACT_TOOL` | `unar` | Archive binary name or full path (`lsar` is derived from it for the integrity test) |
| `EXTRACT_OWNER` | empty | Numeric `uid:gid` for a best-effort chown of extracted files (Unraid = `99:100`); empty skips chown |
| `EXTRACT_MIN_AGE_SECONDS` | `300` | Skip archives whose newest volume is younger than this (settle guard against still-downloading files) |
| `EXTRACT_PROTECT_SECONDS` | `86400` | Keep extracted output undeletable for this long after extraction, so *arr can import it before normal orphan cleanup resumes |
| `PLEX_URL` | empty | Plex server base URL (e.g. `http://192.168.1.10:32400`) |
| `PLEX_TOKEN` | empty | Plex `X-Plex-Token` (sent as a header, never in the URL) |
| `PLEX_SECTIONS` | empty | Comma-separated library section **IDs** to scan; empty ⇒ auto-detect video sections |
| `PLEX_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Plex |
| `PLEX_VERIFY_TLS` | `true` | Verify TLS certificates (set `false` for a self-signed reverse proxy) |
| `PLEX_DUPLICATE_REPORT_PATH` | `/config/plex-duplicates.json` | JSON duplicate report output path |
| `RADARR_URL` | empty | Radarr base URL (e.g. `http://192.168.1.10:7878`); enables movie `*arr`-tracking |
| `RADARR_API_KEY` | empty | Radarr API key (sent as `X-Api-Key`, never in the URL) |
| `RADARR_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Radarr |
| `RADARR_VERIFY_TLS` | `true` | Verify TLS certificates for Radarr |
| `SONARR_URL` | empty | Sonarr base URL (e.g. `http://192.168.1.10:8989`); enables episode `*arr`-tracking |
| `SONARR_API_KEY` | empty | Sonarr API key (sent as `X-Api-Key`, never in the URL) |
| `SONARR_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Sonarr |
| `SONARR_VERIFY_TLS` | `true` | Verify TLS certificates for Sonarr |

> **Note:** the `PLEX_*` variables drive the [Plex Duplicate Report](#plex-duplicate-report) subcommand. They are unused by the `scan`/`service` cleanup commands — leave them empty if you only use qBittorrent cleanup. The optional `RADARR_*`/`SONARR_*` variables add [`*arr`-tracking annotations](#radarrsonarr-tracking-optional) to that report; each is inert unless both its URL and API key are set.

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
  as reclaimable; check them by hand. A mis-stacked pair (two different titles
  Plex split across parts of one `Media`) lists **each** physical file at its
  true size, so both conflicting paths are visible for review.
- **⚠️ arr-tracked (Radarr/Sonarr)** — redundant copies that Radarr/Sonarr
  tracks; see below. Shows a "not configured" hint until you set `RADARR_*` /
  `SONARR_*`.

For a **stacked multi-part copy** — one release Plex splits across several files
(`cd1.mkv` + `cd2.mkv`) that share a `media_id` — the report keeps it as one
logical copy (its size is the sum of the parts, the unit you keep or reclaim) but
also lists every part file with its own true size. In the JSON each copy carries
a `parts` array (`[{file, size}, …]`), always present and single-element for an
unstacked copy, so an operator auditing the report sees an accurate file→size
mapping for every physical file rather than only the first part at the summed
size.

Flags:

| Flag | Purpose |
| --- | --- |
| `--json-only` | Write the JSON report only; suppress the printed table |
| `--limit N` | Cap printed rows per section (the JSON report is unaffected) |
| `--section ID` | Scan a specific library section ID; repeatable; overrides `PLEX_SECTIONS` |

Getting your token: open any item in Plex Web → **⋯ → Get Info → View XML**, and
copy the `X-Plex-Token` value from the URL. It travels as a request header, never
in a logged URL.

### Radarr/Sonarr tracking (optional)

Set `RADARR_URL` + `RADARR_API_KEY` and/or `SONARR_URL` + `SONARR_API_KEY` to
annotate each duplicate copy with whether an `*arr` tracks it. This matters
because **deleting an `*arr`-tracked file makes the `*arr` grab a replacement** —
so the report turns "these copies are redundant" into "these are safe to delete
vs. these will re-download unless you remove them via Radarr/Sonarr":

- **`tracked`** — an `*arr` tracks this exact file. Reclaimable rows that would
  delete a tracked copy are tagged `[arr:tracked]`, and the file is listed in the
  **arr-tracked** section. Delete it via Radarr/Sonarr (or unmonitor first) or it
  re-downloads.
- **`untracked`** — safe to delete (movies only; see below).
- **`unknown`** — could not be confirmed. **Never** treated as safe.

The report **still never deletes anything** — this is annotation only. Each
`*arr` is enabled only when both its URL and API key are set; if one is
unreachable, the report logs a warning and continues (that library's copies fall
back to `unknown`). The token/key travels as an `X-Api-Key` header, never in a
logged URL.

**Matching is asymmetric by design.** Movies join on the TMDB id (Plex's
`tmdb://` guid is exactly Radarr's key), so a movie's redundant copies can be
confidently marked `untracked` (safe). Episodes match by **filename only** —
Plex's episode guids are episode-level, not the series TVDB id Sonarr keys on —
so a TV copy is either `tracked` (Sonarr tracks that filename) or `unknown`, and
is never labeled `untracked`/safe. This is deliberately conservative: the layer
never tells you a TV file is safe unless it can prove Sonarr doesn't track it,
which it can't from filenames alone.

## RAR extraction

Scene releases often arrive as `.rar` (frequently multi-volume) inside a
torrent's download folder, and Radarr/Sonarr cannot import the media until the
archive is extracted. With `EXTRACT_ENABLED=true`, **every `scan`/`service` cycle
detects RAR archives under the watch roots, integrity-tests them, and extracts
them in place** — right before the orphan-deletion pass, so the freshly extracted
media is protected in the same cycle. This folds what used to be a separate
`rar_extractor.sh` cron into the service.

```bash
# Turn it on for the cleanup cycle (dry-run reports would-extract, writes nothing):
EXTRACT_ENABLED=true DRY_RUN=true  unraid-cache-cleaner scan
EXTRACT_ENABLED=true DRY_RUN=false unraid-cache-cleaner scan

# Or run extraction on its own, without the deletion pass:
EXTRACT_ENABLED=true DRY_RUN=false unraid-cache-cleaner extract
```

- **Opt-in and dry-run-safe.** Extraction mutates the download path, so it is off
  unless `EXTRACT_ENABLED=true`, and it honors `DRY_RUN` exactly like deletion.
- **Safe by construction.** Extracted output is recorded as a first-party
  protected input, so the deletion pass never removes media it just extracted —
  even for a single-file `.rar` torrent, an archive sitting loose at the watch
  root, or after the source torrent deregisters. The protection lasts
  `EXTRACT_PROTECT_SECONDS` (default 24h) after extraction, giving `*arr` time to
  import before normal orphan cleanup can reclaim the leftover.
- **Idempotent and claim-safe.** A successful extraction is recorded in the
  SQLite state DB, so later cycles skip it instead of re-extracting; a
  claim-before-extract guard keeps a one-shot `extract` from colliding with a
  running `service`. Failed and deferred archives are not recorded, so they retry.
- **Multi-volume aware.** A `name.partNN.rar` set is extracted once from its first
  volume, not once per part; legacy `name.rar` + `name.rNN` sets extract from the
  `.rar`. A set missing its first volume (still downloading) is left until it
  arrives.
- **Settle guard.** Archives whose newest volume is younger than
  `EXTRACT_MIN_AGE_SECONDS`, or whose source torrent has not finished
  downloading, are deferred and retried. A failed integrity test defers likewise;
  a failed extraction is reported and the archive is kept for a later retry.
- **Ownership.** Set `EXTRACT_OWNER=99:100` (Unraid's `nobody:users`) for a
  best-effort chown of extracted files; a chown failure is a warning, never fatal.
- **Platform.** Extraction is Linux-in-container only — it relies on the bundled
  free `unar`/`lsar` binaries. Local dev on macOS/Windows runs the rest of the
  suite; the one real-binary test skips unless `unar` is installed.

> **Migrating from `rar_extractor.sh`:** set `EXTRACT_ENABLED=true` and remove the
> external cron — the service now extracts on every cycle. The `rar_extractor.sh`
> entry can stay harmlessly in `EXCLUDED_GLOBS` or be dropped.

## Packaging

### Hosted Container

The repo publishes a container image automatically from GitHub Actions to:

```bash
ghcr.io/bwbama85/unraid-cache-cleaner:latest          # every push to main
ghcr.io/bwbama85/unraid-cache-cleaner:vX.Y.Z          # every vX.Y.Z tag
```

`:latest` tracks `main`; pushing a `vX.Y.Z` tag additionally publishes an immutable versioned image (see [Releases](#releases)). The Unraid template pins `:latest`, so no per-release template edit is needed.

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

### Releases

Versioning follows [SemVer](https://semver.org/); the git tag is the single source of truth for a release. Pushing a `vX.Y.Z` tag triggers [`publish.yml`](.github/workflows/publish.yml), which builds and pushes the versioned GHCR image. The first release is `v1.0.0`.

Maintainers cut releases with the **`/release`** Claude Code skill (`.claude/skills/release/`), which runs preflight (on `main`, clean tree, in sync with `origin/main`, CI green on `HEAD`), bumps the version in `pyproject.toml` and `src/unraid_cache_cleaner/__init__.py` (the client `User-Agent` derives from `__version__`, so there is no third string to touch), prepends a `CHANGELOG.md` section, commits + annotated-tags on `main`, pushes, and creates the GitHub Release from that changelog section:

```
/release [patch|minor|major]     # default: patch
/release --version v1.2.0        # explicit version
/release --dry-run minor         # preview the bump + changelog, no commit/tag/push
```

The commit history and per-release notes live in [CHANGELOG.md](CHANGELOG.md).

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
