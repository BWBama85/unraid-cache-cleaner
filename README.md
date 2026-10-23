# Unraid Cache Cleaner

`unraid-cache-cleaner` is a container-first storage-maintenance service for Unraid and other Linux hosts. It can:

- remove files left behind after a torrent is removed from qBittorrent;
- extract RAR releases in the download path so Radarr or Sonarr can import them;
- find and rank duplicate movie and TV files reported by Plex;
- confirm duplicate content with optional partial or full hashing;
- annotate copies with Radarr/Sonarr tracking state; and
- serve the duplicate report in a web UI, with an optional, audited reclaim workflow.

The cleanup, extraction, reporting, and web features are independent. You can run only the qBittorrent cleaner, only the Plex report, or combine them in one container. Destructive features are disabled or dry-run-only by default.

Although the packaged image is designed for Unraid, the CLI also works on compatible Linux/Docker hosts and from a Python 3.9+ environment. The Python runtime has no third-party dependencies; the image also includes `unar`/`lsar` for the optional extraction feature.

## At a Glance

| Capability | How to run it | Default behavior | Can change watched/media files? |
| --- | --- | --- | --- |
| qBittorrent orphan cleanup | `service` or `scan` | `DRY_RUN=true` | Only with `DRY_RUN=false` |
| RAR extraction | cleanup cycle or `extract` | `EXTRACT_ENABLED=false` | Only when enabled and `DRY_RUN=false` |
| Plex duplicate report | `plex-duplicates` | Read-only toward Plex/media; writes its report/cache under `/config` | No |
| Duplicate-report web UI | `web`, or `WEB_ENABLED=true` with `service` | Viewer only | No |
| Browser/API duplicate reclaim | Web UI with `WEB_ENABLE_ACTIONS=true` | `WEB_ACTIONS_DRY_RUN=true` | Only after actions and real deletes are both enabled |

## Safety Model

- Every torrent whose qBittorrent-reported path is correctly mounted in the cleaner is protected, regardless of its download state.
- Multi-file torrent directories are protected as units. With the default parent-protection option, a single-file torrent below a watch root protects its parent directory too.
- Symlinks are never followed, built-in temporary-file globs are always excluded, and operator globs are additive.
- Any file under `WATCH_PATHS` that is not tracked, protected, or excluded becomes an orphan candidate—even if it was never a torrent—so do not aim the cleaner at a mixed-use directory without exclusions.
- An untracked file must pass both the persistent orphan grace period and the minimum file-age check before it is eligible.
- Candidate history, extraction records, and delete audits live in a persistent SQLite database under `/config`.
- Cleanup and extraction default to `DRY_RUN=true`; web reclaims have their own second dry-run switch.
- Plex mismatches, hash-proven different content, the selected keeper, and copies with unknown Radarr/Sonarr association are never reclaimed by the web action layer.
- Failed operations are reported and retried or left for review; there is no quarantine or undo after a successful delete.

No automated classifier can replace reviewing your own paths and reports. Keep both deletion switches in dry-run mode until the output matches your storage layout.

## Contents

- [Quick Start on Unraid](#quick-start-on-unraid)
- [The Path-Mapping Rule](#the-path-mapping-rule)
- [Commands](#commands)
- [How qBittorrent Cleanup Works](#how-qbittorrent-cleanup-works)
- [Configuration](#configuration)
- [Plex Duplicate Report](#plex-duplicate-report)
- [RAR Extraction](#rar-extraction)
- [Packaging and Other Install Methods](#packaging-and-other-install-methods)
- [Operations and Troubleshooting](#operations-and-troubleshooting)
- [Development](#development)
- [Project Layout](#project-layout)
- [Current Scope and Limitations](#current-scope-and-limitations)
- [License](#license)

## Quick Start on Unraid

### 1. Install the template

Run this over SSH on the Unraid server:

```bash
curl -fsSL https://raw.githubusercontent.com/BWBama85/unraid-cache-cleaner/main/scripts/install-unraid-template.sh | bash
```

This copies the bundled Docker template into Unraid's user-template directory. If you prefer to inspect it before running it, see [`scripts/install-unraid-template.sh`](scripts/install-unraid-template.sh).

### 2. Add the container

Open **Docker → Add Container**, select the `unraid-cache-cleaner` template, and configure:

- **Config:** `/mnt/user/appdata/unraid-cache-cleaner` on the host → `/config` in the container.
- **Downloads:** the same host download path qBittorrent uses → the same in-container path qBittorrent reports. The template defaults to `/data`.
- **qBittorrent URL:** use `http://qbittorrent:8080` only when both containers share a custom Docker network with working name resolution. On Unraid's default bridge network, use the Unraid server IP and qBittorrent's published WebUI port instead.
- **qBittorrent username/password:** the WebUI credentials the cleaner should use.
- **Watch paths:** normally `/data`.
- **WebUI port:** choose an unused host port if qBittorrent already publishes `8080`; the container's internal `WEB_PORT` can remain `8080`.
- **Dry run:** leave `DRY_RUN=true`.

The minimum cleanup configuration on Unraid's default bridge looks like this; replace the sample LAN address, published port, and credentials. If you deliberately put both containers on a shared custom network, `http://qbittorrent:8080` is valid instead.

```bash
QBITTORRENT_URL=http://192.168.1.10:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=change-me
WATCH_PATHS=/data
DRY_RUN=true
```

If scripts, logs, or unrelated data also live under the watch root, add them to `EXCLUDED_GLOBS` before starting. An annotated starter file is available at [`.env.example`](.env.example).

### 3. Start in dry-run mode

Start the container. Its default command is `service`: it scans immediately and then repeats every `POLL_INTERVAL_SECONDS` (five minutes by default).

Review both the container log and the latest structured report:

```bash
docker logs --tail 100 unraid-cache-cleaner
cat /mnt/user/appdata/unraid-cache-cleaner/last-run.json
```

The report includes the resolved watch roots, torrent and file counts, orphan candidates, eligible candidates, warnings/errors, and every action that was taken or would be taken. Leave the service in dry-run mode for at least one full `ORPHAN_GRACE_SECONDS` window (six hours by default) and inspect more than one cycle.

Dry-run scans still update the persistent candidate observation window. That is intentional: when you later enable deletion, files that have already remained orphaned for the full grace period may be eligible on the next cycle.

### 4. Enable cleanup

Only after the report contains nothing you need, change:

```bash
DRY_RUN=false
```

Restart the container and continue monitoring `/config/last-run.json`. RAR extraction, the web UI, and web reclaims remain off until separately enabled.

## The Path-Mapping Rule

qBittorrent returns Linux paths such as `/data/Movie/file.mkv`. The cleaner must see that content at the **same path inside its own container**. The host directory may be any location, but both containers must mount the same underlying data and use the same in-container target:

```text
qBittorrent:  /mnt/cache/downloads  ->  /data
Cleaner:      /mnt/cache/downloads  ->  /data
```

This does not mean the two containers must share a network. Filesystem mounts and the qBittorrent API URL are separate concerns:

- the matching mount lets the cleaner resolve qBittorrent's file paths;
- the API URL lets the cleaner reach the qBittorrent WebUI.

Avoid mapping the same host directory to `/data` in one container and `/downloads` in the other. The cleaner deliberately does not guess a qBittorrent-to-cleaner download-path translation.

`WEB_MEDIA_PATH_MAP` is a separate mapping used for Plex-library hashing and untracked web reclaims; it does not fix a mismatched qBittorrent download mount.

## Commands

The image entrypoint is `unraid-cache-cleaner`, and its default command is `service`.

| Command | Purpose | Important requirements |
| --- | --- | --- |
| `service` | Run cleanup immediately, then poll forever; optionally host the web UI | qBittorrent settings and valid watch roots |
| `scan` | Run one cleanup/extraction cycle and exit | Same settings as `service` |
| `extract` | Run only the RAR extraction pass | `EXTRACT_ENABLED=true` and an explicit `WATCH_PATHS` |
| `plex-duplicates` | Generate the read-only Plex JSON/table report | `PLEX_URL` and `PLEX_TOKEN` |
| `web` | Serve the existing Plex report; actions are still controlled separately | Existing report for useful content; `WEB_ENABLE_ACTIONS` controls mutations |

Run `unraid-cache-cleaner --help` or `unraid-cache-cleaner <command> --help` for the built-in CLI reference. In a running container:

```bash
docker exec unraid-cache-cleaner unraid-cache-cleaner --help
docker exec unraid-cache-cleaner unraid-cache-cleaner plex-duplicates --json-only
```

`plex-duplicates` also accepts `--limit N` for terminal rows and repeatable `--section ID` overrides. Omitting a command runs `service`.

## How qBittorrent Cleanup Works

On each `scan` or service cycle the cleaner:

1. authenticates to the qBittorrent WebUI and fetches every torrent;
2. resolves the configured watch roots (or infers them from qBittorrent when `WATCH_PATHS` is empty);
3. builds exact-file and protected-directory rules for tracked content;
4. optionally extracts eligible RAR sets and protects their outputs;
5. walks the watch roots without following symlinks or entering protected directories;
6. synchronizes the current untracked-file set into SQLite;
7. marks files eligible only after both the orphan grace and minimum age pass;
8. reports `would_delete` actions in dry-run mode or removes eligible files otherwise; and
9. optionally removes now-empty directories and writes the latest JSON report.

Explicit `WATCH_PATHS` are strongly recommended on Unraid. If they are empty, the cleaner falls back to qBittorrent's default save path and the save paths in its torrent list; any path that is not actually mounted in the cleaner is skipped, and a run with no valid root fails closed.

### Directory protection behavior

- A multi-file torrent whose content path is a directory protects that whole directory.
- A single-file torrent at the watch root protects only that file.
- Any single-file torrent below a watch root also protects its parent directory when `PROTECT_SINGLE_FILE_PARENT_DIRS=true`.
- Extracted outputs are protected as exact paths for `EXTRACT_PROTECT_SECONDS`, rather than protecting an entire watch root.

The cleaner does not try to prove that the parent is dedicated to that torrent. If a tracked file shares a subdirectory with unrelated files, the whole subdirectory is skipped while this option is enabled. Files directly at the watch root still receive exact-file protection only, so the rule does not disable cleanup across a flat root.

## Configuration

Configuration is environment-variable-only. The table lists the Python runtime defaults; the included `.env.example` and Unraid template explicitly set `WEB_BIND_ADDRESS=0.0.0.0` so a published container port is reachable.

### qBittorrent cleanup and shared runtime

| Variable | Default | Purpose |
| --- | --- | --- |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` | qBittorrent WebUI base URL |
| `QBITTORRENT_USERNAME` | empty | WebUI username; required by the cleanup client |
| `QBITTORRENT_PASSWORD` | empty | WebUI password |
| `QBITTORRENT_TIMEOUT_SECONDS` | `15` | HTTP timeout |
| `QBITTORRENT_VERIFY_TLS` | `true` | Verify TLS certificates |
| `WATCH_PATHS` | empty | Comma-separated mounted roots to scan |
| `POLL_INTERVAL_SECONDS` | `300` | Delay between scans in service mode |
| `ORPHAN_GRACE_SECONDS` | `21600` | How long a file must stay untracked before deletion |
| `MIN_FILE_AGE_SECONDS` | `1800` | Minimum file age before it can be deleted |
| `DRY_RUN` | `true` | Report only, no deletes |
| `DELETE_EMPTY_DIRS` | `true` | Remove empty directories after file deletion |
| `PROTECT_SINGLE_FILE_PARENT_DIRS` | `true` | Protect the parent directory of every tracked single-file torrent below a watch root. This also skips unrelated siblings in a shared subdirectory; files directly at the watch root remain exact-file-only |
| `EXCLUDED_GLOBS` | _(adds to defaults)_ | Extra basename or full-path glob patterns to skip, merged with the built-in defaults |
| `STATE_DB_PATH` | `/config/state.sqlite3` | SQLite state database (WAL mode; also creates `-wal`/`-shm` sidecar files alongside it) |
| `REPORT_PATH` | `/config/last-run.json` | JSON summary of the last run |
| `LOG_LEVEL` | `INFO` | Python log level |
| `HTTP_MAX_ATTEMPTS` | `1` | Attempts per idempotent (GET/HEAD) request to qBittorrent/Plex/Radarr/Sonarr. `1` = no retry (default); raise it (e.g. `3`) to retry a transient 5xx / connection blip with exponential backoff instead of failing the read on the first hiccup. A non-idempotent POST (qBittorrent login) is never retried |

### RAR variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `EXTRACT_ENABLED` | `false` | Enable [RAR extraction](#rar-extraction) in the `scan`/`service` cycle and the `extract` subcommand (opt-in; extraction mutates) |
| `EXTRACT_TOOL` | `unar` | Archive binary name or full path (`lsar` is derived from it for the integrity test) |
| `EXTRACT_OWNER` | empty | Numeric `uid:gid` for a best-effort chown of extracted files (Unraid = `99:100`); empty skips chown |
| `EXTRACT_MIN_AGE_SECONDS` | `300` | Skip archives whose newest volume is younger than this (settle guard against still-downloading files) |
| `EXTRACT_PROTECT_SECONDS` | `86400` | Keep extracted output undeletable for this long after extraction, so *arr can import it before normal orphan cleanup resumes |

### Plex and hashing variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLEX_URL` | empty | Plex server base URL (e.g. `http://192.168.1.10:32400`) |
| `PLEX_TOKEN` | empty | Plex `X-Plex-Token` (sent as a header, never in the URL) |
| `PLEX_SECTIONS` | empty | Comma-separated library section **IDs** to scan; empty ⇒ auto-detect video sections |
| `PLEX_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Plex |
| `PLEX_VERIFY_TLS` | `true` | Verify TLS certificates (set `false` for a self-signed reverse proxy) |
| `PLEX_DUPLICATE_REPORT_PATH` | `/config/plex-duplicates.json` | JSON duplicate report output path |
| `HASH_MODE` | `off` | Opt into [content-hash confirmation](#content-hash-confirmation): `off` (no media read), `partial` (size + first/last 4 MiB), or `full` (whole-file SHA-256). A proven mismatch protects an `identical` group; hashing same-size buckets in an `upgrade` is annotation-only. Requires a media mount and `WEB_MEDIA_PATH_MAP` |
| `HASH_CACHE` | `true` | Cache content-hash digests between runs so `HASH_MODE=partial`/`full` doesn't re-read every media file each report. Keyed by each copy's size, mtime, ctime, and inode (a changed file — even a same-size/same-mtime overwrite — is re-hashed); fail-open (a corrupt/locked cache just re-hashes, never a wrong verdict). Inert when `HASH_MODE=off` (no cache DB is opened). Set `false` to always hash live |
| `HASH_CACHE_PATH` | `/config/hash-cache.sqlite3` | Location of the hash-cache SQLite sidecar (created only when the hash pass runs). Keep it on persistent storage (e.g. `/config`) so the cache survives restarts |

### Radarr and Sonarr variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `RADARR_URL` | empty | Radarr base URL (e.g. `http://192.168.1.10:7878`); enables movie `*arr`-tracking |
| `RADARR_API_KEY` | empty | Radarr API key (sent as `X-Api-Key`, never in the URL) |
| `RADARR_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Radarr |
| `RADARR_VERIFY_TLS` | `true` | Verify TLS certificates for Radarr |
| `SONARR_URL` | empty | Sonarr base URL (e.g. `http://192.168.1.10:8989`); enables episode `*arr`-tracking |
| `SONARR_API_KEY` | empty | Sonarr API key (sent as `X-Api-Key`, never in the URL) |
| `SONARR_TIMEOUT_SECONDS` | `30` | HTTP timeout when querying Sonarr |
| `SONARR_VERIFY_TLS` | `true` | Verify TLS certificates for Sonarr |

### Web variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEB_ENABLED` | `false` | Also serve the [web UI](#web-gui) from the long-running `service` command (opt-in). The standalone `web` subcommand ignores this |
| `WEB_BIND_ADDRESS` | `127.0.0.1` | Address the web UI binds to. The image template and `.env.example` override this to `0.0.0.0` so a published port is reachable |
| `WEB_PORT` | `8080` | TCP port the web UI listens on |
| `WEB_ENABLE_ACTIONS` | `false` | Enable [browser/API reclaim](#browser-and-api-reclaim). Off by default; the viewer has no media-mutation route until enabled |
| `WEB_ACTIONS_DRY_RUN` | `true` | Make reclaim requests report `would_delete` and prevent startup reconciliation from purging staging leftovers. Independent of cleanup/extraction `DRY_RUN`; crash-staged files may still be restored |
| `WEB_ACTION_TOKEN` | empty | Shared secret every reclaim must present (`X-Action-Token` header or `token` JSON field for the API; pasted once in the browser to set an unlock cookie). **Required** to actually delete: with actions enabled but no token, every reclaim is refused |
| `WEB_ACTION_SESSION_SECONDS` | `3600` | Lifetime (seconds) of the browser unlock session — how long after pasting `WEB_ACTION_TOKEN` a confirm can proceed without re-pasting it. Drives both the cookie `Max-Age` and the signed-token expiry. A non-positive value falls back to `3600` |
| `WEB_MEDIA_PATH_MAP` | empty | Comma-separated `plex_path:cleaner_path` pairs, e.g. `/media:/library` when Plex reports `/media/...` and this container mounts the same host share at `/library`. Used by hashing and required for filesystem reclaim; an unmapped path is refused |
| `WEB_ALLOWED_ORIGINS` | empty | Comma-separated external origins allowed to submit browser mutations, e.g. `https://media.example.com`; normally needed behind a TLS-terminating reverse proxy. Empty uses the direct same-origin-vs-`Host` check |
| `WEB_ALLOWED_HOSTS` | empty | Comma-separated hostnames accepted in the request `Host` header. IP literals and `localhost` are always accepted; add names such as `tower.local`. Hostnames in `WEB_ALLOWED_ORIGINS` are added automatically |
| `WEB_ACTION_HISTORY_AUTH` | `false` | Require the reclaim credential to view `/actions` and `/api/actions`, which expose previously deleted paths and sizes. The API accepts the token header or unlock cookie; the HTML view uses the cookie. Requires actions plus a configured token or fails closed. Independent of report auth |
| `WEB_ACTION_REPORT_AUTH` | `false` | Require the reclaim credential to view `/`, `/index.html`, and `/api/report`, which expose media paths and sizes. The API accepts the token header or unlock cookie; the HTML view uses the cookie and offers a no-JavaScript unlock form. Requires actions plus a configured token or fails closed. Independent of history auth |
| `WEB_ACTION_INLINE_SCRIPT` | `false` | Enable nonce-authorized progressive enhancements: select-all/live totals and live rescan-status polling. All flows retain a no-JavaScript fallback |

> **Note:** the cleanup cycle itself ignores `PLEX_*`, `RADARR_*`, and `SONARR_*`. They drive the [Plex report](#plex-duplicate-report), its optional [association layer](#radarr-and-sonarr-association), and browser rescan/reclaim. Leave them empty for a cleanup-only deployment. Each `*arr` integration is enabled only when both its URL and API key are set.

If `WATCH_PATHS` is empty, the service falls back to qBittorrent's default save path plus any `save_path` values currently used by torrents. In practice, explicitly setting `WATCH_PATHS` is better on Unraid.

If your watch root also contains helper scripts, logs, or scratch folders that are not managed by qBittorrent, add them to `EXCLUDED_GLOBS`. Your patterns are merged with a built-in default list (`.DS_Store`, `*.part`, `*.!qB`, and other junk/temp patterns), so the defaults stay in effect and you only list your extras. Patterns without a slash match by basename. Patterns with a slash match against the full path inside the container.

Example:

```bash
EXCLUDED_GLOBS=/data/logs/*,/data/orphaned-files/*,find_duplicates.sh,rar_extractor.sh,video_folders.log
```

### Mount and access reference

| Use case | `/config` | Download root | Plex media library |
| --- | --- | --- | --- |
| Cleanup service | Read/write and persistent | Read/write | Not needed |
| Cleanup in strict dry-run mode | Read/write and persistent | Read-only is sufficient | Not needed |
| RAR extraction | Read/write and persistent | Read/write | Not needed |
| Plex report without hashing | Read/write | Not needed | Not needed |
| Plex report with hashing | Read/write | Not needed | Read-only |
| Standalone viewer with no actions/rescan | Shared report may be read-only | Not needed | Not needed |
| Web rescan | Read/write | Not needed | Not needed unless hashing is enabled; then read-only + mapped |
| Tracked `*arr` reclaim | Read/write | Not needed | No direct media mount needed |
| Untracked filesystem reclaim | Read/write | Not needed for that feature | Read/write |

`WEB_MEDIA_PATH_MAP` translates the path in Plex's `Part.file` response to the path **inside this cleaner container**. It is not necessarily a host-path mapping. For example:

```text
Host share:       /mnt/user/Media
Plex mount:       /mnt/user/Media -> /media
Cleaner mount:    /mnt/user/Media -> /library
Configuration:    WEB_MEDIA_PATH_MAP=/media:/library
```

Mount the cleaner's media path read-only for hashing. Change it to read/write only if you intentionally enable filesystem reclaims. Tracked copies are reclaimed through Radarr/Sonarr and do not require a direct media mount.

The published image currently runs as the container's default root user and does not implement `PUID`/`PGID` variables. `EXTRACT_OWNER` is only a best-effort ownership change for newly extracted files; it does not change the process user.

### Persistent data

Keep `/config` on durable storage. It contains:

- `state.sqlite3` and its WAL/SHM sidecars: orphan first-seen times, cleanup actions, extraction claims/outputs, and web-reclaim audit history;
- `last-run.json`: the latest qBittorrent cleanup report;
- `plex-duplicates.json`: the latest Plex duplicate report;
- `hash-cache.sqlite3` when content hashing is enabled; and
- the duplicate-report regeneration lock sidecar.

Losing `/config` resets the orphan observation history, extraction idempotency records, action history, reports, and hash cache. It does **not** preserve container environment variables, mounts, network settings, command, or port mappings; keep those in the Unraid container definition or your Docker configuration. For a consistent backup, stop the container and copy the whole `/config` directory, including SQLite sidecars.

## Plex Duplicate Report

`plex-duplicates` asks Plex which movie and TV items resolve to multiple files, chooses a best copy, classifies the group, and calculates candidate reclaimable space. It is read-only toward Plex and the media library: it issues GET requests and never deletes, moves, unmonitors, or changes a library item. It does write its JSON report, regeneration lock, and optional hash cache under `/config`.

### Generate a report

Set the Plex base URL and token, then run:

```bash
PLEX_URL=http://192.168.1.10:32400 \
PLEX_TOKEN=your-x-plex-token \
unraid-cache-cleaner plex-duplicates
```

In a configured container:

```bash
docker exec unraid-cache-cleaner unraid-cache-cleaner plex-duplicates --json-only
```

With no section override, the command auto-detects movie and TV libraries. `PLEX_SECTIONS` accepts comma-separated library section IDs. CLI flags are:

| Flag | Purpose |
| --- | --- |
| `--json-only` | Write JSON without printing the terminal table |
| `--limit N` | Limit rows per printed section; JSON is unaffected |
| `--section ID` | Scan one section ID; repeatable and overrides `PLEX_SECTIONS` |

The report is written atomically to `PLEX_DUPLICATE_REPORT_PATH` (default `/config/plex-duplicates.json`), so a viewer never observes a partially written report. If a manual report and a browser-triggered regeneration overlap, a cross-process lock lets only one query Plex; the other exits successfully without duplicating the load.

To find a Plex token, open an item in Plex Web, choose **Get Info → View XML**, and copy the `X-Plex-Token` value. The application sends it as a request header, not in the request URL.

### Ranking and classification

Copies are ranked best-first by resolution, then bitrate, then size. Resolution leads because a smaller modern encode can still be better than a larger low-resolution copy. Files that Plex reports as parts of the same `Media` are treated as one stacked logical copy; their sizes are summed for ranking while every physical part remains visible in JSON and detailed table rows.

| Classification | Meaning | Report treatment |
| --- | --- | --- |
| `identical` | Logical copies have the same normalized resolution and size | Candidate reclaim: keep the top-ranked copy |
| `upgrade` | Copies differ in normalized resolution or size | Candidate reclaim: keep the top-ranked copy |
| `mismatch` | File paths contain two distinct IDs in the same `{imdb-…}`, `{tmdb-…}`, or `{tvdb-…}` namespace | Review only; zero reclaimable bytes |
| `different-content` | Hashing proved an otherwise identical group contains different bytes | Review only; zero reclaimable bytes |

Mismatch detection is deliberately narrow: it catches conflicting external-ID tags embedded in paths within the same namespace. An IMDb tag on one path and a TMDB tag on another is not, by itself, a mismatch. Missing tags and other kinds of title mismatch cannot be proven automatically and still require operator review.

Depending on enabled features, the terminal and web reports show:

- candidate-reclaimable groups, with keeper and redundant copies;
- possible metadata/path mismatches excluded from reclaim;
- hash-proven different-content groups excluded from reclaim;
- Radarr/Sonarr-tracked or unknown copies requiring special handling; and
- overall totals, including an alternate keep-smallest estimate, plus the sections that were scanned.

Treat `reclaimable` as eligible under the report's metadata rules, not as permission to delete blindly. With hashing off, `identical` means equal resolution and size—not proven byte identity. Without Radarr/Sonarr association, the report also cannot know which copy an automation service will replace.

### Radarr and Sonarr association

Set both values for each service you want to use:

```bash
RADARR_URL=http://192.168.1.10:7878
RADARR_API_KEY=...
SONARR_URL=http://192.168.1.10:8989
SONARR_API_KEY=...
```

Each configured integration annotates physical copies and records the relevant `*arr` file ID for drift-safe web reclaims. An unreachable service adds a warning and leaves that library's associations `unknown` rather than failing the Plex report.

| Association | Meaning | Web-action treatment |
| --- | --- | --- |
| `tracked` | Radarr/Sonarr tracks the exact file | Reclaim through that service's API so it does not immediately replace the file |
| `untracked` | Radarr confidently identified a redundant movie copy it does not track | Eligible for mapped filesystem reclaim |
| `unknown` | Tracking state could not be proved | Never auto-reclaimed |

The movie and episode joins are intentionally different:

- **Radarr movies:** Plex TMDB IDs anchor the movie match; a matching basename is `tracked`, while other copies in that confidently matched movie can be `untracked`.
- **Sonarr episodes:** matching is by tracked filename. A matching copy is `tracked`; every other copy remains `unknown`. The report never labels a TV copy `untracked`.

This is a prerequisite for browser reclaim, not just an optional badge. A Plex-only report has no association data, so its copies resolve to `unknown` and the action layer refuses them. Regenerate older reports after enabling `*arr` integration so tracked parts also carry current file IDs.

### Content-hash confirmation

`HASH_MODE` adds an optional read-only media pass:

| Mode | Work | Result |
| --- | --- | --- |
| `off` | No media files are opened | Metadata/size-only report |
| `partial` | File size plus first and last 4 MiB of each part | Constant-cost strong signal; a match is `sample-match`, not proof |
| `full` | Streams each relevant file through SHA-256 | Byte-for-byte `hash-confirmed` verdict |

Hashing never mutates media. For an `identical` group, it can only make the action set more conservative: a mismatch changes the group to `different-content`, sets reclaimable bytes to zero, and removes it from web actions. An unreadable, unmapped, missing, symlinked, size-drifted, or incompatible stacked copy becomes `unhashable`; it remains eligible under the original size-only rules but is never presented as confirmed.

For an `upgrade` group, only same-size buckets containing at least two logical copies are hashed. Their `confirmed`, `sample-match`, `different`, or `unhashable` annotation explains possible redundancy but does not change the keeper or reclaimable totals; unique-size copies are not read.

The media must be mounted and mapped from the exact path Plex reports to the cleaner's path. A read-only mount is sufficient and recommended:

```text
Host:                  /mnt/user/Media
Plex sees it at:       /media
Cleaner sees it at:    /library
WEB_MEDIA_PATH_MAP:    /media:/library
```

```bash
HASH_MODE=partial \
WEB_MEDIA_PATH_MAP=/media:/library \
unraid-cache-cleaner plex-duplicates
```

Without a usable map or mount, hashing is skipped with a warning and the report remains size-only. `HASH_CACHE=true` reuses digests from `HASH_CACHE_PATH` (default `/config/hash-cache.sqlite3`) when file size, mtime, ctime, inode, hash mode, and hash scheme still match. A cache hit rechecks readability. A corrupt, locked, or unwritable cache falls back to live hashing instead of trusting stale data. Set `HASH_CACHE=false` to always read live.

### Web GUI

The `web` command serves the current JSON snapshot as HTML and JSON. A normal page load only reads local files/state; it never contacts Plex, qBittorrent, Radarr, or Sonarr. The separately authorized regeneration action described below can contact Plex from a POST request.

Generate a report first, then start the listener:

```bash
unraid-cache-cleaner plex-duplicates --json-only
WEB_BIND_ADDRESS=0.0.0.0 unraid-cache-cleaner web
```

The application default is `127.0.0.1:8080`. The included Unraid template and `.env.example` set `0.0.0.0:8080` so Docker port publishing works. Use an unused host port, for example `8090:8080`, if another application already owns host port 8080.

Alternatively, set `WEB_ENABLED=true` and the long-running `service` command starts the web server on a background thread. A web bind failure is logged but does not stop cleanup. The standalone `web` command ignores `WEB_ENABLED` and always starts the listener.

Core routes are:

| Route | Methods | Purpose |
| --- | --- | --- |
| `/`, `/index.html` | GET, HEAD | HTML duplicate report |
| `/api/report` | GET, HEAD | `{available, report}` JSON snapshot |
| `/healthz` | GET, HEAD | HTTP listener liveness only |
| `/actions`, `/api/actions` | GET, HEAD | HTML/JSON reclaim audit history |
| `/actions/preview` | POST | Browser dry-run preview |
| `/actions/reclaim` | POST | Browser confirmation submit |
| `/api/reclaim` | POST | Token-authenticated JSON reclaim |
| `/actions/unlock` | POST | Mint a browser unlock session for gated read views |
| `/actions/rescan`, `/api/rescan` | GET, HEAD, POST | Regeneration status or authorized trigger |

Mutation routes are unavailable unless the action layer is enabled. Regeneration routes additionally require Plex credentials. A missing, malformed, or not-yet-generated report renders an empty state rather than causing a server error.

`/healthz` proves only that the optional HTTP listener is answering. It does not prove qBittorrent connectivity, successful cleanup cycles, or report freshness, and the image does not define a Docker `HEALTHCHECK`.

### Browser and API reclaim

Web reclaim is the only feature that can delete Plex-library media. Start with this minimum:

```bash
WEB_ENABLE_ACTIONS=true
WEB_ACTIONS_DRY_RUN=true
WEB_ACTION_TOKEN=<long-random-secret>
```

Run the standalone `web` command, or also set `WEB_ENABLED=true` when using `service`; enabling actions alone does not start a listener.

Generate a token locally, for example:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`DRY_RUN` and `WEB_ACTIONS_DRY_RUN` are independent. The first controls qBittorrent cleanup and extraction. The second controls web reclaim deletion and purging of staging residue during startup reconciliation; crash recovery may still restore a staged file to its original name. Keep both true until you have reviewed their separate output.

Before a web reclaim can succeed:

1. Generate a current Plex report with the relevant Radarr/Sonarr integration enabled.
2. Keep `WEB_ACTIONS_DRY_RUN=true` and use the browser preview to verify selected targets.
3. For a `tracked` copy, keep the relevant `*arr` credentials configured; deletion is routed through its API.
4. For an `untracked` Radarr movie copy, mount the media read/write and configure `WEB_MEDIA_PATH_MAP` from the Plex path to the cleaner path.
5. Leave `unknown` copies for manual review; Sonarr's unmatched episode copies are always unknown.
6. Set `WEB_ACTIONS_DRY_RUN=false` only when you intend the confirmation step or JSON request to delete real media.

The action service resolves every submitted `{rating_key, part_id}` against a freshly loaded server-side report. It refuses a stale report timestamp, a missing keeper, the keeper itself, shared keeper paths, `mismatch`, `different-content`, `unknown` associations, malformed parts, and unmapped paths. Client-supplied paths, sizes, associations, and backends are never trusted.

- **Tracked copy:** the saved Radarr/Sonarr file ID is fetched immediately before delete; a missing ID, changed basename, or changed positive size is refused with a regenerate hint.
- **Stacked tracked copy:** all parts are prevalidated before any delete, but the Radarr/Sonarr DELETE requests then run sequentially and cannot be rolled back. If a later request fails, earlier parts remain deleted and the partial result is audited.
- **Untracked filesystem copy:** every part must resolve below a configured media root and still be a regular, non-symlink file of the expected size.
- **Stacked filesystem copy:** all parts are validated, renamed to in-place `.uncc-reclaim` siblings, and only then unlinked. A staging failure rolls back before anything is deleted.
- **Recovery:** web startup reconciles leftover staging siblings. Dry-run prevents purge deletions, but recovery may rename a crash-staged file back to its original path.
- **Audit:** real deletes and partial failures are recorded in `state.sqlite3`; successful filesystem deletion has no quarantine or undo.

The browser flow is always two-step: selection posts to a forced dry-run preview, then **Confirm** reruns all checks. A valid token mints an HMAC-signed `HttpOnly`, `SameSite=Strict` unlock session for `WEB_ACTION_SESSION_SECONDS`; rotating the token invalidates existing sessions. The JSON API accepts the raw token in `X-Action-Token` (preferred) or a top-level `token` field in the JSON body; it does not use the browser unlock cookie for reclaim.

Example API request while `WEB_ACTIONS_DRY_RUN=true`:

```bash
curl -X POST http://192.168.1.10:8090/api/reclaim \
  -H 'Content-Type: application/json' \
  -H "X-Action-Token: $WEB_ACTION_TOKEN" \
  -d '{"report_generated_at": 1720000000.0, "targets": [{"rating_key": "900", "part_id": 2}]}'
```

Replace `report_generated_at`, `rating_key`, and `part_id` with values from the current `/api/report` or `plex-duplicates.json`. A stale timestamp is intentionally refused with `409`.

### Web exposure and authentication

The built-in server has no native TLS, multi-user accounts, or password database. Keep it on a trusted network or place it behind a correctly configured TLS reverse proxy. It emits no external assets, escapes Plex-supplied content, and uses a restrictive Content Security Policy; optional enhancement scripts are nonce-scoped.

- The Python default bind is loopback. The Unraid/example configuration binds all interfaces.
- IP-literal and `localhost` Host headers are accepted. Any hostname such as `tower.local` must appear in `WEB_ALLOWED_HOSTS`; hostnames from `WEB_ALLOWED_ORIGINS` are also admitted.
- A browser mutation on a non-loopback bind must be same-origin. Behind a TLS-terminating reverse proxy, add the external origin such as `https://media.example.com` to `WEB_ALLOWED_ORIGINS`.
- `X-Forwarded-*` headers are not trusted for Host/origin decisions.
- Service clients refuse redirects to another host and HTTPS-to-HTTP downgrades so qBittorrent cookies, Plex tokens, and Radarr/Sonarr API keys stay on the configured host. `HTTP_MAX_ATTEMPTS` retries only GET/HEAD reads; qBittorrent login POSTs and Radarr/Sonarr DELETEs are never retried automatically.
- Report and action-history views expose absolute media paths and sizes. They are LAN-readable by default; enable `WEB_ACTION_REPORT_AUTH=true` and `WEB_ACTION_HISTORY_AUTH=true` to gate both behind the reclaim credential.
- The read-view auth switches fail closed when actions or the token are missing. `/healthz` remains unauthenticated but is still subject to the Host allow-list.

`WEB_ACTION_INLINE_SCRIPT=true` adds nonce-authorized progressive enhancement: select-all/live totals on the reclaim form and live regeneration-status polling. All browser flows continue to work without JavaScript.

### Regenerate from the browser

The application does not schedule Plex reports automatically. Generate them manually, from an external cron/User Scripts schedule, or from the web action below.

When `WEB_ENABLE_ACTIONS=true` and `PLEX_URL`/`PLEX_TOKEN` are available to the web process, the report page shows **Regenerate report**. It uses the same token/session, Host checks, and origin checks as reclaim, but it reads services and deletes no media.

The trigger returns immediately while a background thread queries Plex and optional `*arr` services. Only one regeneration runs at a time in-process, and the report lock prevents a simultaneous CLI/cron run from duplicating it. Publication is atomic; a failed regeneration leaves the prior report intact.

```bash
curl -X POST http://192.168.1.10:8090/api/rescan \
  -H "X-Action-Token: $WEB_ACTION_TOKEN"

curl http://192.168.1.10:8090/api/rescan \
  -H "X-Action-Token: $WEB_ACTION_TOKEN"
```

### Action history

`GET /actions` renders recent `web-reclaim:*` rows; `GET /api/actions` returns them as `{available, actions}`. They show the time, backend, status, bytes, original path, and detail for real reclaims and reconciliation. Preview-only dry runs and validation refusals are not recorded as deletes. The reader reuses one long-lived, query-only SQLite connection and caps results at 200; a missing or legacy database returns `available: false`.

## RAR Extraction

With `EXTRACT_ENABLED=true`, each `scan` or `service` cycle discovers RAR sets below the watch roots, integrity-tests them with `lsar`, and extracts them in place with `unar` before orphan deletion. The published image already contains both tools.

```bash
# Preview integrated extraction and cleanup:
EXTRACT_ENABLED=true DRY_RUN=true unraid-cache-cleaner scan

# Enable integrated extraction and cleanup:
EXTRACT_ENABLED=true DRY_RUN=false unraid-cache-cleaner service

# Run extraction without the qBittorrent cleanup pass:
EXTRACT_ENABLED=true DRY_RUN=false WATCH_PATHS=/data unraid-cache-cleaner extract
```

- Extraction is off by default and honors the main `DRY_RUN` switch. A dry run reports `would_extract` and writes no archive output.
- Successful archives are recorded in SQLite so later cycles do not extract them again. Identity-aware claims prevent a standalone `extract` process and `service` from doing the same work concurrently.
- Fresh outputs are protected as exact paths in the current cleanup cycle and for `EXTRACT_PROTECT_SECONDS` afterward (24 hours by default), giving Radarr/Sonarr time to import them without protecting an entire watch root.
- `name.partNN.rar` and legacy `name.rar` + `name.rNN` sets are treated as one archive. A missing first volume, failed integrity test, or unsettled set is deferred and retried.
- Integrated `scan`/`service` also defers archives beneath a torrent that qBittorrent reports as incomplete. Standalone `extract` has no qBittorrent client; it relies on explicit `WATCH_PATHS`, `EXTRACT_MIN_AGE_SECONDS`, first-volume detection, and the integrity test.
- Archive members are listed before extraction so nested output paths are recorded precisely. `unar` runs in place with force-overwrite enabled and recursive extraction of nested archives disabled. Review dry-run output and the target directory because an archive member can replace an existing same-named file.
- `EXTRACT_OWNER=99:100` performs a best-effort chown of produced files for the common Unraid `nobody:users` ownership. It does not change the container process user.

If you are migrating from an external `rar_extractor.sh` cron, enable this feature, observe a dry run, and remove the old cron so two extractors do not race. The old filename may remain in `EXCLUDED_GLOBS`, but it is no longer required.

## Packaging and Other Install Methods

### Published container

GitHub Actions publishes:

```text
ghcr.io/bwbama85/unraid-cache-cleaner:latest
ghcr.io/bwbama85/unraid-cache-cleaner:vX.Y.Z
ghcr.io/bwbama85/unraid-cache-cleaner:sha-<commit>
```

`latest` follows every push to `main` and is not signed. Release tags are SemVer builds; tag signing began with `v1.1.0`. A registry tag can be moved, so pin the image digest when strict reproducibility matters. The current workflow builds the GitHub-hosted runner's Linux/amd64 image and does not declare a multi-platform build.

### Docker service

Copy [`.env.example`](.env.example) to a private host file and replace its example credentials and paths. Docker reads that file with `--env-file`; the Python application itself does not automatically load `.env` files.

If you want to use a qBittorrent container name in `QBITTORRENT_URL`, create or reuse a user-defined network and attach qBittorrent to it first:

```bash
docker network create media
docker network connect media qbittorrent
```

```bash
docker run -d \
  --name unraid-cache-cleaner \
  --restart unless-stopped \
  --network media \
  --env-file /opt/unraid-cache-cleaner.env \
  -v /srv/unraid-cache-cleaner:/config:rw \
  -v /srv/downloads:/data:rw \
  ghcr.io/bwbama85/unraid-cache-cleaner:latest service
```

The `--network media` example assumes qBittorrent is on the same user-defined Docker network and `QBITTORRENT_URL` uses its container name. Otherwise remove that option and set the URL to a host/LAN IP plus qBittorrent's published WebUI port.

To expose the optional web UI, add `-p 8090:8080` and set `WEB_BIND_ADDRESS=0.0.0.0`. Here `8090` is an arbitrary unused host port and `8080` must match `WEB_PORT` inside the container. Dockerfile `EXPOSE` metadata does not publish the port on its own.

A minimal Compose service is equivalent:

```yaml
services:
  cleaner:
    image: ghcr.io/bwbama85/unraid-cache-cleaner:latest
    container_name: unraid-cache-cleaner
    restart: unless-stopped
    command: service
    env_file:
      - /opt/unraid-cache-cleaner.env
    volumes:
      - /srv/unraid-cache-cleaner:/config:rw
      - /srv/downloads:/data:rw
    networks:
      - media
    # Add only when WEB_ENABLED=true:
    # ports:
    #   - "8090:8080"

networks:
  media:
    external: true
```

The external `media` network must already exist and qBittorrent must also be attached to it. If you use a host/LAN IP in `QBITTORRENT_URL`, remove the Compose `networks` blocks.

Build the current checkout locally with:

```bash
docker build -t unraid-cache-cleaner .
```

### Source installation

There is no PyPI publishing workflow. Install from a clone with Python 3.9 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mkdir -p .local
export STATE_DB_PATH="$PWD/.local/state.sqlite3"
export REPORT_PATH="$PWD/.local/last-run.json"
export PLEX_DUPLICATE_REPORT_PATH="$PWD/.local/plex-duplicates.json"
export HASH_CACHE_PATH="$PWD/.local/hash-cache.sqlite3"
unraid-cache-cleaner --help
```

The path overrides avoid the container-oriented `/config` defaults on a normal workstation. Install `unar`/`lsar` locally only if you intend to use extraction or run its real-binary test.

### Unraid

Use the [Quick Start](#quick-start-on-unraid). Additional deployment background is in [docs/unraid.md](docs/unraid.md), and the bundled template is [contrib/unraid-cache-cleaner.xml](contrib/unraid-cache-cleaner.xml); the README is the current reference for newly added options.

## Operations and Troubleshooting

### Verify a healthy cleanup cycle

A running container is not proof that cleanup is succeeding. Service mode logs a failed cycle, sleeps for the poll interval, and tries again while the container remains running.

Verify all three:

1. logs contain a recent `Run complete` summary rather than repeating connection or mount errors;
2. `/config/last-run.json` has a recent timestamp and sensible torrent, watch-root, scanned-file, candidate, and eligible counts; and
3. dry-run actions match files you genuinely expect to be orphaned.

The optional `/healthz` route only checks HTTP-listener liveness. It does not validate qBittorrent, Plex, a cleanup cycle, or report freshness.

### Common problems

| Symptom | Check |
| --- | --- |
| `Name or service not known` for qBittorrent | Put both containers on the same user-defined network, or use the Unraid/LAN IP and published WebUI port |
| No valid watch roots | Confirm the host bind exists and the cleaner's in-container target exactly matches the path qBittorrent reports |
| Live torrent data appears orphaned | Stop at dry-run; compare both containers' mounts and qBittorrent `content_path` values |
| Candidates exist but eligible count is zero | Wait for both `ORPHAN_GRACE_SECONDS` and `MIN_FILE_AGE_SECONDS`; the first observation starts the grace clock |
| Web request by hostname returns 403 | Add the hostname, without a scheme, to `WEB_ALLOWED_HOSTS`; direct IP and localhost are accepted automatically |
| Reverse-proxied browser POST returns 403 | Add the external `https://host[:port]` origin to `WEB_ALLOWED_ORIGINS` |
| Viewer is empty or stale | Run/schedule `plex-duplicates`; page GETs do not regenerate reports |
| Regenerate button/routes are absent | Enable actions and provide the web process with `PLEX_URL` and `PLEX_TOKEN` |
| Hashing is skipped or shows `unhashable` | Verify the exact Plex-path-to-cleaner-path map, media mount, read permission, current file size, regular-file type, and stacked layout |
| Extraction remains deferred | Check archive age, first volume, integrity, and—in integrated mode—qBittorrent completion state |
| Permission denied | Confirm `/config` and every mutating target are read/write; hashing alone only needs media read access |

### Non-destructive diagnostic scripts

The repository contains three host-side scripts; they do not delete watched media and are not copied into the container image. Clone/download the repository on the Unraid host, then run them from `scripts/`. The example requires host `git`; `fresh-check.sh` also requires host `python3` and updates the cleaner's dry-run state/report.

| Script | Purpose |
| --- | --- |
| `inspect-mounts.sh` | Compare cleaner/qBittorrent networks and `/data` host sources |
| `diagnose-unraid.sh` | Compare current qBittorrent torrents with delete actions in an existing cleanup report |
| `fresh-check.sh` | Force a fresh scan, summarize would-delete paths, and inspect hardlink counts; refuses to run unless `DRY_RUN` is true |

```bash
git clone https://github.com/BWBama85/unraid-cache-cleaner.git
cd unraid-cache-cleaner/scripts
CLEANER=unraid-cache-cleaner QBIT=qbittorrent bash inspect-mounts.sh
CONTAINER=unraid-cache-cleaner bash diagnose-unraid.sh
CONTAINER=unraid-cache-cleaner bash fresh-check.sh
```

These scripts are diagnostic aids, not a substitute for reviewing the full report. `inspect-mounts.sh` assumes the conventional `/data` destination, and `diagnose-unraid.sh` needs an existing `last-run.json`.

### Upgrades, backups, and rollback

- Back up the entire `/config` directory with the container stopped before a major upgrade or downgrade.
- On Unraid, use the Docker update flow to pull and recreate the image. Refreshing the XML template does not automatically add newly introduced optional variables to an already-saved container; add desired opt-ins in **Edit Container**.
- With Docker/Compose, pull the chosen image and recreate the container while preserving the same mounts and environment configuration.
- Pin a release tag for predictable upgrades or a digest for strict reproducibility. To roll back, recreate with the previous tag/digest and the same `/config`; retain the pre-upgrade backup in case an older version cannot interpret newer state.
- State schema additions are applied automatically on startup. Do not delete `state.sqlite3` merely to update—the database carries the cleanup grace history and audits.

### Verify a signed release

Tagged images from `v1.1.0` onward are signed keylessly with GitHub Actions OIDC. `latest` and `v1.0.0` are not signed. Verify a release tag with [cosign](https://docs.sigstore.dev/):

```bash
cosign verify \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --certificate-identity-regexp "(?i)^https://github\.com/BWBama85/unraid-cache-cleaner/\.github/workflows/publish\.yml@refs/tags/v" \
  ghcr.io/bwbama85/unraid-cache-cleaner:vX.Y.Z
```

## Development

The quality gate is the standard-library unittest suite:

```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests -v
# or:
make test
```

CI runs on Python 3.9, 3.11, and 3.12 on Linux and installs `unar` for the real extraction test. Most tests use local fakes and require no live qBittorrent, Plex, Radarr, or Sonarr services.

Handled CLI outcomes are generally:

| Exit status | Meaning |
| --- | --- |
| `0` | Success, clean shutdown, intentionally skipped concurrent Plex generation, or disabled standalone extraction |
| `2` | qBittorrent/Plex/`*arr` client error (also used by `argparse` for invalid CLI syntax) |
| `3` | Runtime failure such as no usable watch root or failure to start standalone web |

Invalid environment values and unexpected errors may exit nonzero with a traceback. Logs are the authoritative detail.

See [docs/architecture.md](docs/architecture.md) for component design, [CHANGELOG.md](CHANGELOG.md) for release history, and the [issue tracker](https://github.com/BWBama85/unraid-cache-cleaner/issues) for bugs and feature requests. Do not include API keys, tokens, passwords, or private media paths in public issues.

## Project Layout

- `src/unraid_cache_cleaner/`: CLI, cleanup planner/service, API clients, extraction, hashing, duplicate analysis/reporting, state, and web/action code
- `tests/`: unit and integration-style tests using local fakes and temporary files
- `docs/architecture.md`: design and failure-model notes
- `docs/unraid.md`: detailed Unraid deployment and diagnostics
- `contrib/unraid-cache-cleaner.xml`: Unraid container template
- `scripts/`: template installer, diagnostics, and release/changelog helpers
- `.github/workflows/`: CI and GHCR publication/signing

## Current Scope and Limitations

- qBittorrent is polled; push events are not used.
- Plex reports are manual, externally scheduled, or browser-triggered; cleanup polling does not automatically generate them.
- The web server is intended for a trusted LAN and has no native TLS or multi-user account system.
- Completed cleanup and media deletes have no quarantine or undo.
- There is no metrics endpoint or Docker health check.
- The published workflow currently targets Linux/amd64 rather than an explicit multi-platform image.

## License

MIT
