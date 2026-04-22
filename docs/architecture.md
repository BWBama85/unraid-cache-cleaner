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
- inspect archive extraction tools directly

