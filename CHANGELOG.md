# Changelog

All notable changes to `unraid-cache-cleaner` are documented here. This project
follows [Semantic Versioning](https://semver.org/); the first tagged release
will be `v1.0.0`. Releases are cut with the `/release` skill
(`.claude/skills/release/`), which prepends each version's section below.

<!-- release sections are inserted below this line, newest first -->

## [1.1.0] - 2026-07-21

### Highlights

The first feature release after v1.0.0 turns the read-only Plex report into a
safe, browser-driven way to *reclaim* duplicates, adds an opt-in RAR-extraction
stage to the cleanup cycle, and hardens the HTTP/state plumbing underneath —
still stdlib-only, still fail-closed by default.

**Opt-in RAR extraction.** A new extraction stage can unpack RAR archives found
in the download path as part of the normal cleanup cycle, so completed archives
are expanded and their leftovers reclaimed in one pass. It is deletion-safe by
construction: extracted media is protected from the orphan sweep until its
replacement is fully in place, output detection is precise — it lists real
archive members rather than guessing, including nested members (#39, #43, #54) —
and an extraction ledger tracks identity and ownership through a bounded crash
window so a mid-run failure never leaves media unprotected (#41, #105). Built up
across #31, #35, #36, #37, #38.

**Plex duplicate web GUI + reclaim action layer.** The `plex-duplicates` report
gains a web viewer (#34) and then a fail-closed action layer that can actually
reclaim the duplicates you choose (#34 Phase 2) — with a two-step confirmation,
a browser unlock session, and CSRF-hardened forms (#62, #63, #68). Reclaims are
made atomic even when copies are stacked across parts (#64), orphaned staging
siblings are reconciled (#72, #73), and a rescan can be triggered from the
browser (#74, #77). The surface is hardened for real-network exposure: a
DNS-rebinding Host allow-list, opt-in auth for the report and action-history
views, and a nonce'd inline script (#67, #79, #80, #82, #83, #85).

**Content-hash duplicate confirmation.** An optional content-hash pass confirms
that flagged duplicates are byte-identical before you trust a reclaim (#9),
backed by a persistent hash cache and a web-viewer hash badge (#92, #94) and
extended to confirm same-size buckets inside upgrade groups (#93).

***arr-aware reclaims.** Radarr/Sonarr file ids are serialized for O(1),
drift-safe reclaims, stacked multi-part copies are represented faithfully in the
report, and *arr error rows carry the part id (#17, #25, #56, #61, #72, #73).

**Reliability & infrastructure.** The shared JSON-HTTP base gained transport
retry/backoff and a non-dict JSON guard, and state moved to a WAL-mode SQLite
store (#20, #24, #45, #50). Report generation is cheaper and now surfaces
stacked reclaim parts (#19, #48).

**Release & dev flow.** `/release` gained an auto-changelog helper and cosign
image signing, and its consequential writes are now enforced by tracked
`permissions.ask` rules so the release checkpoint can't be silently bypassed
(#29, #30, #33, #51).

### Changes

#### Features

- **hasher:** confirm same-size buckets inside upgrade groups (#93) (85e8045)
- **web:** abort a hung rescan poll and surface a degrade hint (#100, #99) (70c9066)
- **web:** bound the rescan live-poll's failure retries with a terminal navigation (#96) (3e61d23)
- **web:** opt-in JS live-poll of the rescan status surface (#90) (ab76879)
- **plex:** persistent hash cache + web viewer hash badge (#92, #94) (2b72464)
- **plex:** optional content-hash pass to confirm byte-identical duplicates (#9) (fda6e06)
- **web:** browser rescan + audit-disambiguated staging sweep (#77, #74) (9dd1a1b)
- **web:** opt-in report-surface auth + optional nonce'd inline script (#85, #80) (8c1b7bc)
- **web:** opt-in auth for the action-history views + document replay-until-expiry session (#82, #83) (e804413)
- **web:** DNS-rebinding Host allow-list + session robustness (#67, #79) (8868030)
- **web:** two-step reclaim confirmation + browser unlock session (#62, #68) (bddd803)
- **web_actions:** reconcile orphaned staging siblings + part_id in *arr error row (#72, #73) (002b8af)
- **web_actions:** make stacked filesystem reclaims atomic (#64) (cfef7b1)
- **arr:** serialize *arr file id for O(1) drift-safe reclaims + untracked stacked table rows (#61, #56) (ad62313)
- **web:** CSRF-harden reclaim form + read-only action-history viewer (#62, #63) (e5685b9)
- **web:** fail-closed action layer to reclaim Plex duplicates (#34 Phase 2) (3a15a81)
- **web:** read-only Plex duplicate report web viewer (#34) (926e498)
- **http,state:** transport retry/backoff, non-dict JSON guard, WAL state store (#50, #45) (27c8097)
- **extractor:** precise output detection via archive member list (#43, #39) (752b8cc)
- **release:** auto-changelog helper, image signing, checkpoint caveat (#29, #30, #33) (37f8c31)
- **plex-report:** represent stacked multi-part copies faithfully (#17, #25) (e2db6c1)
- **extractor:** fold RAR extraction into the cleanup cycle with deletion-safe protection (#35, #36, #37, #38) (9fd8592)
- **extractor:** add opt-in RAR extraction foundation (#31) (095fab4)

#### Bug Fixes

- **extractor:** snapshot unconditionally so outputs stay honest (#105) (47b19dd)
- **web:** embed poll-script constants as escaped JS literals (#100, #99) (a4930ca)
- **plex,web:** guard nested MediaContainer shape (#57) + round-trip-safe copy anchor (#87) (5783fe1)
- **web:** failed unlock reflects can_unlock verdict (codex + agy review, #85) (10dc7d0)
- **web_actions:** refuse a non-ASCII session cookie instead of crashing (#68) (e7c572e)
- **extractor:** mirror -no-recursion when listing members (#43) (feca124)
- **extractor:** harden extraction ledger — identity, ownership token, bounded crash window (#41) (2ad8322)

#### Performance

- **plex-report:** cut report-generation cost + surface stacked reclaim parts (#19, #48) (bb17c5b)

#### Refactors

- **web_actions:** extract shared delete-and-audit loop (#70) (781b454)
- **http:** share a JSON-HTTP base; harden diagnose script redirect (#20, #24) (7377565)
- **plex-report:** derive keeper parts from the ranked pair (#17) (02ac9cc)

#### Tests

- **extractor:** prove nested members are precisely detected (#54) (84c464a)
- **cli:** cover the web-startup staging-reconciliation wiring (#72) (37c6e25)
- **extractor:** gate real lsar test on lsar presence (#43) (baa7a1e)

#### Chores

- **release:** enforce the /release checkpoint with tracked permissions.ask rules (#51) (0a64d1c)

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
