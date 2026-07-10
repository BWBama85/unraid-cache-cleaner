# Unraid Deployment

## Recommended Model

Run `unraid-cache-cleaner` as its own container and mount the same download path qBittorrent uses at the same internal path.

Published image:

```text
ghcr.io/bwbama85/unraid-cache-cleaner:latest
```

## Fastest Install

Run this on your Unraid server over SSH:

```bash
curl -fsSL https://raw.githubusercontent.com/BWBama85/unraid-cache-cleaner/main/scripts/install-unraid-template.sh | bash
```

That installs the XML template into Unraid's user template folder. After that:

1. Open the Docker tab.
2. Click Add Container.
3. Select `unraid-cache-cleaner`.
4. Set your qBittorrent URL, username, password, and host download path.
5. Start with `DRY_RUN=true`.

If you prefer to inspect the script first, it lives at [scripts/install-unraid-template.sh](../scripts/install-unraid-template.sh).

Example:

- qBittorrent writes to `/data`
- host path is `/mnt/cache/downloads`
- this container also mounts `/mnt/cache/downloads` at `/data`

That is the cleanest way to avoid path translation bugs.

## Required Mounts

### `/config`

Persistent state and the latest JSON report.

Suggested host path:

```text
/mnt/user/appdata/unraid-cache-cleaner
```

### Download roots

Mount the qBittorrent save path, ideally at the same internal path qBittorrent sees.

Suggested example:

```text
Host: /mnt/cache/downloads
Container: /data
```

## Required Environment

```text
QBITTORRENT_URL=http://qbittorrent:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=change-me
WATCH_PATHS=/data
EXCLUDED_GLOBS=/data/logs/*,/data/orphaned-files/*,find_duplicates.sh,rar_extractor.sh,video_folders.log
DRY_RUN=true
```

If your mounted download root also contains non-torrent files you want to keep, exclude them explicitly. This matters when you keep helper scripts or log folders directly under `/data`.

`EXCLUDED_GLOBS` is **added to** a built-in default list (`.DS_Store`, `Thumbs.db`, `*.part`, `*.!qB`, and other junk/temp patterns), so you only need to list your own extras — the defaults stay in effect either way.

Patterns without a slash match by basename. Patterns with a slash match the full in-container path.

> **RAR extraction is now first-party.** If you previously ran `rar_extractor.sh`
> on a cron to unpack scene releases, set `EXTRACT_ENABLED=true` and remove that
> cron — the service now detects and extracts RAR archives every cycle and
> protects the extracted media from cleanup (see the README's [RAR
> extraction](../README.md#rar-extraction) section). The `rar_extractor.sh` entry
> in `EXCLUDED_GLOBS` above is now optional; it excludes a script you no longer
> need and can be dropped.

## Recommended First Run

Use these safety settings first:

```text
DRY_RUN=true
ORPHAN_GRACE_SECONDS=21600
MIN_FILE_AGE_SECONDS=1800
DELETE_EMPTY_DIRS=true
PROTECT_SINGLE_FILE_PARENT_DIRS=true
```

For a qB root that also contains helper files, add:

```text
EXCLUDED_GLOBS=/data/logs/*,/data/orphaned-files/*,find_duplicates.sh,rar_extractor.sh,video_folders.log
```

Let it run in dry-run mode for a while. Review `/config/last-run.json`. Once the results are clean, switch `DRY_RUN=false`.

## Community Applications Template

A starter XML template is included at [contrib/unraid-cache-cleaner.xml](../contrib/unraid-cache-cleaner.xml). It already points at the published GHCR image and repo URLs. The install script above copies this file into Unraid's standard user-template location for you. You will still need to set:

- qBittorrent URL
- credentials
- the correct host paths
- any `EXCLUDED_GLOBS` needed to keep non-torrent files under the watch root

## Web GUI (read-only Plex duplicate report)

The container can serve the Plex duplicate report as a web page (see the README's
[Web GUI](../README.md#web-gui-for-the-duplicate-report) section). It is a
**read-only** viewer — it displays an existing report and never scans or deletes.

To reach it from this Unraid container:

1. Generate a report first (it only *displays* one): run the `plex-duplicates`
   subcommand — e.g. as a User Scripts cron, or by temporarily setting the
   container's command to `plex-duplicates` — with `PLEX_URL` + `PLEX_TOKEN` set.
   The report is written to `PLEX_DUPLICATE_REPORT_PATH` (default
   `/config/plex-duplicates.json`).
2. Light up the viewer one of two ways:
   - **Same container:** set `WEB_ENABLED=true`. The long-running `service` then
     also serves the viewer on `WEB_PORT` (default `8080`). The template's
     **WebUI** link points at the mapped port.
   - **Separate container:** add a second copy of this image with its
     **Post Arguments** / command set to `web`. Mount the same `/config` so it
     reads the report the first container wrote.
3. The template maps host port → container `8080` (**WebUI Port**). Open the
   container's WebUI, or `http://<unraid-ip>:<mapped-port>/`.

The viewer has no authentication — like qBittorrent/Plex/`*arr`, it assumes a
trusted LAN. It is read-only, so there is no delete button to misfire; acting on
duplicates from the browser is a planned, fail-closed follow-up.

## Common Misconfiguration

### qBittorrent uses `/data`, cleaner uses `/downloads`

Avoid this unless you also add a path-mapping layer. This project intentionally assumes same-path mounting because it is the least error-prone setup on Unraid.

### Flat watch root full of single-file torrents

The cleaner can still work, but it has less context for protecting extracted output next to active single-file torrents. Dedicated per-job subdirectories are better.

### Credentials left empty

The service will not use container-local unauthenticated access. Configure WebUI credentials.

## Troubleshooting

Three read-only scripts in [`scripts/`](../scripts) help diagnose a cleaner that connects to qBittorrent but flags the wrong files (or nothing). Run them on the Unraid server. None of them delete anything.

| Script | What it checks |
| --- | --- |
| `inspect-mounts.sh` | Both containers' networks and `/data` host paths side by side. Run as `CLEANER=<name> QBIT=<name> bash inspect-mounts.sh` if your container names differ. |
| `diagnose-unraid.sh` | Queries the qBittorrent API from inside the cleaner container and reports whether any flagged orphan is actually a live torrent. |
| `fresh-check.sh` | Forces a fresh dry-run, prints what it would delete, and shows hardlink counts so you can see whether deleting each file is safe. Refuses to run unless `DRY_RUN=true`. |

Common real-world causes of a broken or misleading run:

- **`Name or service not known`** — `QBITTORRENT_URL` uses a Docker container name (e.g. `http://qbittorrent:8080`) but the cleaner is on the plain `bridge` network, where container-name DNS does not resolve, or the qBittorrent container has a different name. Use the Unraid host IP and the published WebUI port instead, e.g. `http://192.168.1.10:8080`, or put both containers on the same user-defined Docker network.
- **Every torrent looks like an orphan / `/data` appears empty** — the cleaner's `/data` mount points at a different host path than qBittorrent's. On Unraid this often means one container mounts a cache-only path (`/mnt/cache/...`) while the other mounts the user share (`/mnt/user/...`), or simply a different folder. Mount the cleaner's `/data` at the exact same host path qBittorrent uses.
- **`qBittorrent login failed:` with an empty message** — qBittorrent is bypassing authentication for the cleaner's subnet/localhost and returns an empty `204` from `/api/v2/auth/login`. Update to a build that accepts the bypass response (handled since the auth-bypass fix).
