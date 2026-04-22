# Unraid Deployment

## Recommended Model

Run `unraid-cache-cleaner` as its own container and mount the same download path qBittorrent uses at the same internal path.

Published image:

```text
ghcr.io/bwbama85/unraid-cache-cleaner:latest
```

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
DRY_RUN=true
```

## Recommended First Run

Use these safety settings first:

```text
DRY_RUN=true
ORPHAN_GRACE_SECONDS=21600
MIN_FILE_AGE_SECONDS=1800
DELETE_EMPTY_DIRS=true
PROTECT_SINGLE_FILE_PARENT_DIRS=true
```

Let it run in dry-run mode for a while. Review `/config/last-run.json`. Once the results are clean, switch `DRY_RUN=false`.

## Community Applications Template

A starter XML template is included at [contrib/unraid-cache-cleaner.xml](../contrib/unraid-cache-cleaner.xml). It already points at the published GHCR image and repo URLs. You will still need to set:

- your image repository
- qBittorrent URL
- credentials
- the correct host paths

## Common Misconfiguration

### qBittorrent uses `/data`, cleaner uses `/downloads`

Avoid this unless you also add a path-mapping layer. This project intentionally assumes same-path mounting because it is the least error-prone setup on Unraid.

### Flat watch root full of single-file torrents

The cleaner can still work, but it has less context for protecting extracted output next to active single-file torrents. Dedicated per-job subdirectories are better.

### Credentials left empty

The service will not use container-local unauthenticated access. Configure WebUI credentials.
