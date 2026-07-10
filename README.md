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
| `STATE_DB_PATH` | `/config/state.sqlite3` | SQLite state database (WAL mode; also creates `-wal`/`-shm` sidecar files alongside it) |
| `REPORT_PATH` | `/config/last-run.json` | JSON summary of the last run |
| `LOG_LEVEL` | `INFO` | Python log level |
| `HTTP_MAX_ATTEMPTS` | `1` | Attempts per idempotent (GET/HEAD) request to qBittorrent/Plex/Radarr/Sonarr. `1` = no retry (default); raise it (e.g. `3`) to retry a transient 5xx / connection blip with exponential backoff instead of failing the read on the first hiccup. A non-idempotent POST (qBittorrent login) is never retried |
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
| `WEB_ENABLED` | `false` | Also serve the [web UI](#web-gui-for-the-duplicate-report) from the long-running `service` command (opt-in). The standalone `web` subcommand ignores this |
| `WEB_BIND_ADDRESS` | `0.0.0.0` | Address the web UI binds to. `0.0.0.0` so a mapped container port is reachable; set `127.0.0.1` to restrict to loopback |
| `WEB_PORT` | `8080` | TCP port the web UI listens on |
| `WEB_ENABLE_ACTIONS` | `false` | Enable the [action layer](#reclaiming-duplicates-from-the-browser-phase-2) so an operator can delete redundant copies from the browser. Off by default; the viewer is read-only until you set this |
| `WEB_ACTIONS_DRY_RUN` | `true` | When actions are on, report what a reclaim *would* delete and touch nothing (mirrors `DRY_RUN`). Set `false` only after reviewing a dry run |
| `WEB_ACTION_TOKEN` | empty | Shared secret every reclaim must present (`X-Action-Token` header or the form field). **Required** to actually delete: with actions enabled but no token, every reclaim is refused |
| `WEB_MEDIA_PATH_MAP` | empty | Comma-separated `plex_prefix:container_prefix` pairs mapping Plex-reported paths to this container's mounts, e.g. `/mnt/user/Media:/media`. Needed for a filesystem delete of an *untracked* copy; an unmapped path is refused |
| `WEB_ALLOWED_ORIGINS` | empty | Comma-separated allow-list of external origins (e.g. `https://media.example.com`) that may submit the reclaim form. Set this behind a TLS-terminating reverse proxy, where the server sees plain HTTP and can't trust `Host` to infer the external scheme. Empty uses the same-origin-vs-`Host` check (the LAN default). See [CSRF/origin hardening](#reclaiming-duplicates-from-the-browser-phase-2) |

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

- **Reclaimable (safe)** — duplicate/upgrade copies you can safely remove. On a
  Plex-only run (no `RADARR_*`/`SONARR_*`), a reclaimable copy that is a
  **stacked** multi-part release lists each part file at its true size as
  indented sub-rows; a single-file copy stays a one-line summary.
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
size. In the printed table the per-part breakdown appears in the mismatch review,
the arr-tracked section, and — on a Plex-only run — the Reclaimable (safe) section.

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

### Web GUI for the duplicate report

The `web` subcommand serves the duplicate report in a browser instead of the
terminal. By default it is a **read-only viewer**: it reads the on-disk report at
`PLEX_DUPLICATE_REPORT_PATH` and renders the same three sections the table shows
(reclaimable, mismatch review, `*arr`-tracked), plus a JSON API. It never runs a
scan. An opt-in [action layer](#reclaiming-duplicates-from-the-browser-phase-2)
(off unless `WEB_ENABLE_ACTIONS=true`) adds the ability to reclaim selected
redundant copies from the browser.

```bash
# Generate (or refresh) the report first — the viewer only displays it:
PLEX_URL=http://192.168.1.10:32400 PLEX_TOKEN=your-x-plex-token \
  unraid-cache-cleaner plex-duplicates --json-only

# Then serve it (default 0.0.0.0:8080):
PLEX_DUPLICATE_REPORT_PATH=/config/plex-duplicates.json \
  unraid-cache-cleaner web
```

Open `http://<host>:8080/`. Routes:

| Route | Method | Serves |
| --- | --- | --- |
| `/` | GET | The HTML report (totals, reclaimable, mismatch review, `*arr`-tracked) |
| `/api/report` | GET | `{"available": bool, "report": <report JSON or null>}` |
| `/actions` | GET | Read-only [action-history](#action-history) page — what the browser layer has deleted |
| `/api/actions` | GET | `{"available": bool, "actions": [...]}` — the recent `web-reclaim:*` audit rows |
| `/healthz` | GET | `ok` (liveness) |
| `/api/reclaim` | POST | Reclaim endpoint (JSON) — only when actions are enabled; `405` otherwise |
| `/actions/reclaim` | POST | Browser-form reclaim — only when actions are enabled |

- **Read-only by default, fail-closed.** Until `WEB_ENABLE_ACTIONS=true` no
  mutation endpoint exists and every non-`GET` verb returns `405`. All
  Plex-supplied strings (titles, paths, warnings) are HTML-escaped, the page ships
  no external assets under a strict `Content-Security-Policy`, and a
  missing/truncated/malformed report renders an empty state rather than a `500`.
- **Runs standalone or beside cleanup.** Run it as its own container/command
  (`web`), or set `WEB_ENABLED=true` so the long-running `service` also serves the
  web UI on a background thread — one container that both cleans up and shows the
  report. It is off by default, so `service` gains no listener unless you opt in.
- **LAN-scoped.** Like qBittorrent/Plex/`*arr`, the UI assumes a trusted LAN and
  has no user accounts; it binds `0.0.0.0` by default so a mapped container port is
  reachable. Set `WEB_BIND_ADDRESS=127.0.0.1` to restrict it to loopback. The
  action layer adds a shared-token gate on top (see below).

#### Reclaiming duplicates from the browser (Phase 2)

Setting `WEB_ENABLE_ACTIONS=true` lights up a reclaim path so you can select
redundant copies in the browser and delete them. This is the project's first
outside-triggered deletion of *library* media, so it is fail-closed on every axis:

- **Disabled and dry-run by default.** Actions are off until you opt in, and even
  then `WEB_ACTIONS_DRY_RUN=true` (the default) reports what *would* be deleted and
  touches nothing. Set `WEB_ACTIONS_DRY_RUN=false` only after reviewing a dry run.
- **Token-gated.** Every reclaim must present `WEB_ACTION_TOKEN` (via the
  `X-Action-Token` header or the form's token field). Enabling actions **without**
  a token refuses every request — there is never an unauthenticated delete endpoint.
- **CSRF/origin hardened.** On top of the token, a same-origin check defends against
  a cross-site request forgery. It scales with the bind address:
  - **Loopback bind (`127.0.0.1`, the code default):** behavior is unchanged — a
    form POST with no `Origin` is accepted (the token still gates it).
  - **Non-loopback bind (`0.0.0.0`, the container default):** a browser reclaim
    **form** must carry a matching `Origin` (or same-origin `Referer`); a cross-site
    form POST is refused *even if it omits `Origin`*. The **JSON API** stays
    token-only when it sends no `Origin`, so `curl`/scripts keep working.
  - **Behind a TLS reverse proxy:** the server sees plain HTTP and can't trust the
    client `Host` to infer the external `https` scheme, so set `WEB_ALLOWED_ORIGINS`
    to the external origin(s) the browser sends (e.g. `https://media.example.com`).
- **Honors the report's safety signals.** The keeper is never deleted, a `mismatch`
  group (Plex merged different titles) is never reclaimed, and an `unknown`
  association is never auto-deleted. Targets are resolved against a *fresh*
  server-side report snapshot — a page built on a stale report is refused (`409`) —
  and a copy's size/path are re-validated immediately before deletion (TOCTOU).
- **Routed by association.** An `untracked` copy is a filesystem delete, which
  requires `WEB_MEDIA_PATH_MAP` to map the Plex path to a *mounted* container path
  (unmapped → refused, because the Plex library is not mounted by default). A
  `tracked` copy is deleted via Radarr/Sonarr (so it does not immediately
  re-download); its `*arr` file id is resolved live and refused if missing or
  ambiguous.
- **Audited.** Every real delete (and any partial failure) is written to the
  SQLite state store's `actions` table, so you can answer "what did the GUI delete".

```bash
# Reclaim via the JSON API (dry run shown; echo the report's generated_at):
curl -X POST http://<host>:8080/api/reclaim \
  -H 'Content-Type: application/json' -H "X-Action-Token: $WEB_ACTION_TOKEN" \
  -d '{"report_generated_at": 1720000000.0, "targets": [{"rating_key": "900", "part_id": 2}]}'
```

Because it deletes media, keep it bound to a trusted LAN, keep a token set, and
prefer routing tracked copies through Radarr/Sonarr over raw filesystem deletes.

#### Action history

Every real delete (and any partial failure) the browser layer makes is recorded in
the SQLite state store's `actions` table, so you can answer "what did the GUI
delete". Two **read-only** views surface it:

- **`GET /actions`** — a page listing the most recent reclaim deletes, newest first
  (time, backend, status, bytes reclaimed, path, detail). The report page links to
  it whenever the state store is reachable.
- **`GET /api/actions`** — `{"available": bool, "actions": [...]}` for scripting.

These read only the `web-reclaim:*` rows (not the cleaner's own deletes), over a
short-lived read-only connection that never creates or migrates the database — so
they work even after you turn actions back off, and a missing/legacy store simply
reports `available: false`. Dry-run previews and refusals aren't deletes, so they
are not recorded.

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

To serve the [read-only duplicate-report viewer](#web-gui-for-the-duplicate-report),
run the `web` command and publish the port (the report must already exist under
`/config`). Set `WEB_BIND_ADDRESS=0.0.0.0` so the server listens on the
container's published interface — the default is loopback-only, which a mapped
host port cannot reach:

```bash
docker run --rm -p 8080:8080 \
  -e WEB_BIND_ADDRESS=0.0.0.0 \
  -v /mnt/user/appdata/unraid-cache-cleaner:/config \
  ghcr.io/bwbama85/unraid-cache-cleaner:latest web
```

### Unraid

See [docs/unraid.md](docs/unraid.md). A starter container template is included at [contrib/unraid-cache-cleaner.xml](contrib/unraid-cache-cleaner.xml), and the default repository path is the published GHCR image.

### Releases

Versioning follows [SemVer](https://semver.org/); the git tag is the single source of truth for a release. Pushing a `vX.Y.Z` tag triggers [`publish.yml`](.github/workflows/publish.yml), which builds and pushes the versioned GHCR image and **signs it** (see [Verifying the release image](#verifying-the-release-image)). The first release is `v1.0.0`.

Maintainers cut releases with the **`/release`** Claude Code skill (`.claude/skills/release/`), which runs preflight (on `main`, clean tree, in sync with `origin/main`, CI green on `HEAD`), bumps the version in `pyproject.toml` and `src/unraid_cache_cleaner/__init__.py` (the client `User-Agent` derives from `__version__`, so there is no third string to touch), prepends a `CHANGELOG.md` section, commits + annotated-tags on `main`, pushes, and creates the GitHub Release from that changelog section:

```
/release [patch|minor|major]     # default: patch
/release --version v1.2.0        # explicit version
/release --auto-changelog        # draft the changelog from git log instead of authoring it
/release --dry-run minor         # preview the bump + changelog, no commit/tag/push
```

The changelog is authored by hand by default; `--auto-changelog` opts into a stdlib-only helper ([`scripts/generate_changelog.py`](scripts/generate_changelog.py)) that groups `git log` by conventional-commit type into a deterministic draft you refine before committing. The commit history and per-release notes live in [CHANGELOG.md](CHANGELOG.md).

### Verifying the release image

Each `vX.Y.Z` release image is signed keyless with [cosign](https://docs.sigstore.dev/) using GitHub Actions OIDC — no long-lived keys or extra secrets. Verify a pulled tag against the signing workflow's identity:

```bash
cosign verify \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --certificate-identity-regexp "(?i)^https://github\.com/BWBama85/unraid-cache-cleaner/\.github/workflows/publish\.yml@refs/tags/v" \
  ghcr.io/bwbama85/unraid-cache-cleaner:vX.Y.Z
```

A successful verification prints the signed claims; a tampered or unsigned image fails. Signing was added after `v1.0.0`, so it applies to releases cut from that point on.

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
