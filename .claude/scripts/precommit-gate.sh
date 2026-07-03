#!/bin/bash
# Stop-hook quality gate for /implement-issue and other feature-branch sessions.
#
# Claude Code invokes this when the agent tries to end its turn. We block the
# stop (exit 2) if the test suite would fail on the current branch — so the
# autonomous flow can't accidentally finalize work while tests are red.
#
# This is the Python port of getrich's precommit-gate.sh. That repo runs
# `pnpm typecheck && pnpm lint && pnpm test`; unraid-cache-cleaner is stdlib-only
# with no typecheck/lint tooling, so the gate is the unittest suite plus a fast
# `compileall` syntax check to catch obvious breakage before the slower tests.
#
# No-op conditions (exit 0):
#   - HEAD is `main` (we never commit there anyway; the gate would just slow
#     /clear and ordinary conversational turns).
#   - The branch diff vs origin/main, plus staged/unstaged/untracked changes,
#     contains zero `src/` or `tests/` files (docs-only / .claude-only turns).
#
# When a check fails we exit 2 so Claude Code treats the stop as blocked, per the
# hooks-exit-code contract (1 is non-blocking, 2 blocks). A red test suite on a
# feature branch is genuinely wrong-until-fixed, so it stays a hard exit-2 block.

set -u

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$repo_root" ]; then
  exit 0
fi
cd "$repo_root"

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
if [ "$branch" = "main" ] || [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
  exit 0
fi

# Collect changed files relative to origin/main, falling back to local main.
base_ref="origin/main"
if ! git rev-parse --verify --quiet "$base_ref" >/dev/null 2>&1; then
  base_ref="main"
fi

committed_diff=""
if git rev-parse --verify --quiet "$base_ref" >/dev/null 2>&1; then
  committed_diff="$(git diff --name-only "${base_ref}...HEAD" 2>/dev/null || true)"
fi
staged_diff="$(git diff --name-only --cached 2>/dev/null || true)"
unstaged_diff="$(git diff --name-only 2>/dev/null || true)"
# Untracked files don't show up in `git diff`, but a turn that creates a brand-new
# src/foo.py or tests/bar.py is exactly what the gate exists to catch.
untracked="$(git ls-files --others --exclude-standard 2>/dev/null || true)"

changed="$(printf '%s\n%s\n%s\n%s\n' "$committed_diff" "$staged_diff" "$unstaged_diff" "$untracked" | sort -u | sed '/^$/d')"
if [ -z "$changed" ]; then
  exit 0
fi

if ! printf '%s\n' "$changed" | grep -Eq '^(src|tests)/'; then
  exit 0
fi

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  printf 'precommit-gate: python3 not on PATH; cannot run the test suite\n' >&2
  exit 2
fi

run_check() {
  local label="$1"
  shift
  if ! "$@" >/tmp/precommit-gate-"$label".log 2>&1; then
    tail -c 4000 /tmp/precommit-gate-"$label".log >&2 || true
    printf '\nprecommit-gate: %s failed (see /tmp/precommit-gate-%s.log)\n' "$label" "$label" >&2
    return 1
  fi
  return 0
}

failed=""
run_check compileall "$PY" -m compileall -q src tests || failed="${failed}compileall "
run_check test "$PY" -m unittest discover -s tests || failed="${failed}test "

if [ -n "$failed" ]; then
  printf '\nprecommit-gate: blocking stop — fix: %s\n' "$failed" >&2
  exit 2
fi

exit 0
