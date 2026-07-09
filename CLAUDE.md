# CLAUDE.md

Guidance for Claude Code (and any agent) working in this repo.

## What this is

`unraid-cache-cleaner` is a small, **stdlib-only Python** companion service for Unraid. It polls the qBittorrent WebUI API and safely deletes leftover files from a mounted download path after a grace window, and (in progress) reports Plex library duplicates so disk space can be reclaimed. It ships as a Docker image (`python:3.12-slim`) and an Unraid Community Applications template.

## Architecture

Single package: `src/unraid_cache_cleaner/`.

| Module | Responsibility |
| --- | --- |
| `cli.py` | `argparse` subcommands (`scan`, `service`) + `main()` dispatch and exit codes |
| `config.py` | `Config` frozen dataclass, `from_env()`, env parsing helpers |
| `models.py` | Frozen data records (`TorrentRecord`, `FileRecord`, …) + mutable `RunReport` |
| `http_client.py` | Shared `urllib` JSON-HTTP base (`JsonHttpClient`): fail-closed opener, request/JSON path, error taxonomy |
| `http_redirect.py` | Fail-closed `HostBoundRedirectHandler` the base installs on every opener |
| `qbittorrent.py` | Minimal `urllib` WebUI client + `QbittorrentClientError` |
| `scanner.py` | `os.walk` filesystem scan, symlink/glob skipping |
| `planner.py` | Protection plan, path normalization, orphan detection |
| `service.py` | Orchestration: scan cycle, deletion, JSON report, logging |
| `state.py` | SQLite candidate/action persistence |

Tests live in `tests/` and run with `unittest`.

## Conventions — follow these strictly

- **Stdlib only. No third-party runtime dependencies.** `pyproject.toml` has no `[project.dependencies]`; keep it that way. HTTP is `urllib`, not `requests`.
- **`Config` is a frozen dataclass with NO field defaults** (`config.py`), constructed field-by-field in tests. **Append new fields with defaults** so existing `Config(...)` calls and `from_env` keep working; wire each into `from_env()` and, if it's a persisted path, `ensure_directories()`.
- **New external services subclass `http_client.JsonHttpClient`:** the shared `urllib` base owns opener construction (fail-closed redirect guard + TLS-verify toggle), the request/read/JSON-decode path, and the `HTTPError`/`URLError`/`OSError` → `*ClientError` taxonomy. A new client sets `service_name` + `error_class` (a custom `SomethingClientError(RuntimeError)`), supplies its credential via `_auth_headers()` (never in a URL), and overrides an `_on_*_error` hook only for a status-specific message. qBittorrent additionally uses `_extra_handlers()` (cookie jar) and keeps its own `_request` for the form-POST login + 403 reauth. No login secrets in logs.
- **`pathlib.Path` everywhere** (not string paths). Reuse `planner.normalize_path`.
- **Per-module loggers:** `LOGGER = logging.getLogger(__name__)`. One compact structured summary line per run.
- **Safety-first / fail-closed:** default to the safe mode (`DRY_RUN=true`; the Plex report is **read-only**). Missing credentials or mounts should stop with a clear message, not guess.
- **Sparse comments** — code is self-documenting; comment only subtle logic. Full type hints; `from __future__ import annotations`.

## Dev commands

```bash
# Test suite (this is the quality gate)
python3 -m unittest discover -s tests -v
# Fast syntax check
python3 -m compileall -q src tests
# Run locally
PYTHONPATH=src python3 -m unraid_cache_cleaner scan
```

There is **no** typecheck or lint tooling (stdlib-only, minimal). The Stop-hook gate runs `compileall` + `unittest`.

## Testing patterns

- `unittest.TestCase`, `tempfile.TemporaryDirectory()` for filesystem isolation.
- Inject a **fake client** rather than mocking HTTP — see `FakeClient` in `tests/test_service.py`. New service layers should take their client via constructor so tests pass a fake.
- Tests add `src/` to `sys.path` at the top of the file.

## Git & PR workflow

- **Never commit to `main`.** Feature branch (`issue-<num>-<slug>`) + PR always. The **one** sanctioned exception is the `chore(release): vX.Y.Z` commit made by the `/release` skill (see the skills list below).
- **Semantic commits**, referencing the issue. End commit messages with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Never `--no-verify`; never destructive git** (`reset --hard`, `push --force`, `clean -fd`) unless the user explicitly asks.
- Docs ship with the change: any user/operator-facing change updates `README.md` (+ its Configuration table), `.env.example`, `contrib/unraid-cache-cleaner.xml`, and/or `docs/*` in the same PR.

## Agent workflow — skills & gates (ported from the `getrich` project)

Two Stop hooks are wired in `.claude/settings.json`:

- **`.claude/scripts/precommit-gate.sh`** — blocks ending a turn on a feature branch if `compileall`/`unittest` fail (no-op on `main` and on docs-only/`.claude`-only turns).
- **`.claude/scripts/implement-issue-gate.sh`** — during a `/implement-issue` run, keeps the turn going until a PR is open or the run is declared blocked. State lives in `.claude/state/` (gitignored).

Skills (`.claude/skills/`):

- **`/implement-issue <num> [more nums] [hints]`** — end-to-end: preflight → codex gap-analysis → branch → implement + tests → gate → commit → `/code-review` → triage → push → PR → **file follow-up issues for all deferred work** → completion checklist. Never stops until a PR exists or it's genuinely blocked.
- **`/resolve-pr-threads <PR#>`** — resolve bot-authored review threads after the PR is up.
- **`/release [patch|minor|major] [--auto-changelog] | --version vX.Y.Z | --dry-run [bump]`** — cut a versioned release: preflight (on `main`, clean, in-sync, CI green on HEAD) → release-goal gate → bump `pyproject.toml` + `__init__.py` → prepend `CHANGELOG.md` (authored by default; `--auto-changelog` drafts it from `git log` via `scripts/generate_changelog.py`) → `chore(release)` commit + annotated tag on `main` (the sole sanctioned exception to never-commit-`main`) → push (tag triggers `publish.yml`, which cosign-signs the versioned image) → `gh release create` → surface the tag's GHCR publish run → roll the `Next release` milestone. First cut is `v1.0.0`. **Release checkpoint caveat:** the three consequential writes (`git push`, annotated `git tag`, `gh release create`) prompt only if *no* permission scope pre-approves them — and a broad `.claude/settings.local.json` grant **or the skill's own bare-`Bash` `allowed-tools`** silently defeats that (Claude Code precedence is deny→ask→allow; `allow` from any scope skips the prompt). Keep local `gh release`/`git push` grants narrowly scoped; the robust fix is a tracked `permissions.ask` rule for the three writes (tracked follow-up).
- Issue authoring uses the **user-level `/create-issue`** skill (11-axis gap analysis before filing).

## Release goals — milestones

Follow-up / planned work is milestoned:

- **`Next release`** — slices/dependencies of the current release goal. Add the **`release-blocker`** label when the goal is genuinely incomplete without the item.
- **`Backlog`** — tangential, speculative, or post-cut work.

Every filed issue gets exactly one milestone (`/implement-issue` step 12 enforces this).

## Environment notes

Service endpoints, tokens, and LAN specifics are **not** stored in this repo — they come from env vars (`QBITTORRENT_*`, `PLEX_*`, `RADARR_*`, `SONARR_*`) and live in `.env` (gitignored). Secrets never go in tracked files.
