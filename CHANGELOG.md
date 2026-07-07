# Changelog

All notable changes to `unraid-cache-cleaner` are documented here. This project
follows [Semantic Versioning](https://semver.org/); the first tagged release
will be `v1.0.0`. Releases are cut with the `/release` skill
(`.claude/skills/release/`), which prepends each version's section below.

<!-- release sections are inserted below this line, newest first -->

## [1.0.0] - 2026-07-07

### Highlights

First stable release of `unraid-cache-cleaner` — a small, stdlib-only companion
service for Unraid that reclaims disk space two ways, always defaulting to the
safe mode.

**qBittorrent cache cleanup.** The core service polls the qBittorrent WebUI API,
walks a mounted download path, and safely deletes leftover orphan files after a
grace window. It ships fail-closed: `DRY_RUN=true` by default, missing
credentials or mounts stop with a clear message rather than guessing, and
symlinks and excluded globs are skipped. `EXCLUDED_GLOBS` is configurable and
merges with a set of built-in defaults (#3), and the qBittorrent auth-bypass
login response is handled for LAN setups (#1). Ships as a `python:3.12-slim`
Docker image with an Unraid Community Applications template.

**Plex duplicate reporting (read-only).** A new `plex-duplicates` subcommand
surfaces duplicate copies across your Plex libraries so you can reclaim space,
built up in layers: a stdlib `urllib` Plex API client and duplicate models
(#5), a duplicate-analysis engine with ranking, classification, and reclaimable
math (#6), a subcommand with both JSON and human-readable report output (#7),
Radarr/Sonarr-tracked-copy flagging so you know which duplicate the *arr layer
manages (#8), and a dedupe fix that surfaces intra-stack mismatches instead of
silently dropping them (#14). The entire Plex surface is read-only.

**Security hardening.** All HTTP clients now refuse cross-host and
TLS-downgrade redirects so credentials and the `X-Plex-Token` never leave the
box (#12, #22).

**Infrastructure & developer flow.** CI runs the `unittest` suite and publishes
the container image on tag push (GHCR), on Node 24 actions. The repo carries a
Claude Code dev-flow — `/implement-issue` and `/resolve-pr-threads` skills with
Stop-hook quality gates (#11) — plus this `/release` skill that single-sources
the version across `pyproject.toml` and `__init__.py` (#10), and a read-only
release-inspection command allow-list (#28).

### Changes

- chore(perms): allow-list read-only release-inspection commands (#28) (c3ef49d)
- feat(release): add /release skill and single-source the version (#10) (e2c17df)
- fix(dedupe): surface intra-stack mismatches instead of dropping them (#14) (0ed63ff)
- fix(clients): refuse cross-host/TLS-downgrade redirects so credentials stay on-box (#22) (1a7057c)
- fix(plex): refuse cross-host/TLS-downgrade redirects so X-Plex-Token stays on-box (#12) (e76e880)
- feat(plex): flag Radarr/Sonarr-tracked duplicate copies in the report (#8) (467dbd0)
- feat(plex): add plex-duplicates subcommand with JSON + human report (#7) (7a48c97)
- feat(plex): add duplicate analysis engine (ranking, classification, reclaimable) (#6) (dde29ad)
- feat(plex): add Plex API client, config, and duplicate models (#5) (c29917d)
- chore: add Claude Code dev-flow (implement-issue + resolve-pr-threads, gates, CLAUDE.md) (22e5775)
- feat: merge EXCLUDED_GLOBS with built-in defaults (#3) (3ffa300)
- docs: add read-only diagnostic scripts and troubleshooting guide (#2) (1d3a5e1)
- fix: accept qBittorrent auth-bypass login response (#1) (f44a207)
- feat: document and template EXCLUDED_GLOBS (de26bc0)
- fix: make container subcommands runnable (eb1d507)
- feat: streamline unraid installation (5d23cd1)
- ci: move workflows to node 24 actions (449eee1)
- ci: automate tests and container publishing (c0f2802)
- feat: initial unraid cache cleaner (6a77d54)
