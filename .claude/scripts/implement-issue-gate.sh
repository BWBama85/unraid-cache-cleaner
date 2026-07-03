#!/bin/bash
# Stop-hook workflow-completion gate for /implement-issue runs.
#
# Claude Code invokes this when the agent tries to end its turn. We keep the
# turn going when an /implement-issue run is in progress AND the PR for it has
# not been opened yet AND the run has not been declared blocked. This is the
# hard backstop for the "no-stop-until-PR" invariant — soft guardrails (skill
# prose, memory) have proven insufficient on their own.
#
# Ported from getrich; the only project-specific change is the "uncommitted
# changes" grep (apps|packages -> src|tests). The marker/version/gh machinery is
# unchanged.
#
# How we keep the turn going (getrich issue #208): this is a SOFT "you're not
# done, keep working" cue. On Claude Code >= 2.1.163 we deliver it via
# `hookSpecificOutput.additionalContext` on stdout + exit 0 (non-error feedback);
# older builds fall back to legacy `stderr + exit 2`. Version is read from
# CLAUDE_CODE_EXECPATH (the exact running binary), not `claude --version`.
#
# State protocol (written by .claude/skills/implement-issue/SKILL.md):
#   .claude/state/implement-issue-active.json   — marker for an in-flight run
#   .claude/state/implement-issue-blocked.json  — written by the skill ONLY on
#                                                  documented legitimate stops.

set -u

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$repo_root" ]; then
  exit 0
fi
cd "$repo_root"

# Return 0 when the *running* Claude Code honors
# hookSpecificOutput.additionalContext on the Stop event — added in v2.1.163.
# Read from CLAUDE_CODE_EXECPATH (the binary serving THIS session), anchoring on
# the 'versions' path segment. When undeterminable, return 1 so the caller uses
# the safe legacy exit-2 block.
stop_hook_additional_context_supported() {
  local execpath="${CLAUDE_CODE_EXECPATH:-}"
  [ -n "$execpath" ] || return 1

  local -a parts
  IFS='/' read -r -a parts <<< "$execpath"
  local ver="" prev="" seg
  for seg in "${parts[@]}"; do
    if [ "$prev" = "versions" ]; then
      ver="$seg"
      break
    fi
    prev="$seg"
  done

  case "$ver" in
    [0-9]*.[0-9]*.[0-9]*) : ;;
    *) return 1 ;;
  esac

  awk -v v="$ver" -v min="2.1.163" '
    BEGIN {
      nv = split(v, V, "."); nm = split(min, M, ".");
      n = (nv > nm) ? nv : nm;
      for (i = 1; i <= n; i++) {
        a = (i <= nv) ? V[i] + 0 : 0;
        b = (i <= nm) ? M[i] + 0 : 0;
        if (a > b) exit 0;
        if (a < b) exit 1;
      }
      exit 0;
    }'
}

marker=".claude/state/implement-issue-active.json"
blocked=".claude/state/implement-issue-blocked.json"

if [ ! -f "$marker" ]; then
  exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
  printf 'implement-issue-gate: jq not on PATH; cannot parse %s — passing\n' "$marker" >&2
  exit 0
fi
if ! jq -e . "$marker" >/dev/null 2>&1; then
  printf 'implement-issue-gate: %s is not valid JSON — passing; delete it if stale\n' "$marker" >&2
  exit 0
fi

marker_branch="$(jq -r '.branch // ""' "$marker")"
marker_issue="$(jq -r '.issue // ""' "$marker")"
marker_phase="$(jq -r '.phase // "unknown"' "$marker")"
marker_pr_url="$(jq -r '.prUrl // ""' "$marker")"

current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

# Unrelated turn — leave the marker untouched so the original run can resume.
if [ -n "$marker_branch" ] && [ "$marker_branch" != "$current_branch" ]; then
  exit 0
fi

# Legitimate stop declared by the skill — but ONLY if the blocked file actually
# references this run (branch or issue must match the active marker).
if [ -f "$blocked" ]; then
  blocked_legit="no"
  if jq -e . "$blocked" >/dev/null 2>&1; then
    blocked_branch="$(jq -r '.branch // ""' "$blocked")"
    blocked_issue="$(jq -r '.issue // ""' "$blocked")"
    if { [ -n "$blocked_branch" ] && [ "$blocked_branch" = "$marker_branch" ]; } \
       || { [ -n "$blocked_issue"  ] && [ "$blocked_issue"  = "$marker_issue"  ]; }; then
      blocked_legit="yes"
    fi
  fi
  if [ "$blocked_legit" = "yes" ]; then
    exit 0
  fi
  printf 'implement-issue-gate: ignoring stale %s (branch/issue does not match active marker)\n' \
    "$blocked" >&2
fi

# PR opened per the marker, OR run explicitly marked complete — past the
# invariant threshold. Clean up and pass.
if [ -n "$marker_pr_url" ] || [ "$marker_phase" = "complete" ]; then
  rm -f "$marker" 2>/dev/null || true
  exit 0
fi

# Last guard: even if the skill forgot to write prUrl, check GitHub directly so
# a real open PR for this branch unblocks the gate.
if command -v gh >/dev/null 2>&1; then
  gh_err="$(mktemp .claude/state/gh-err.XXXXXX 2>/dev/null || echo .claude/state/gh-err.$$)"
  pr_url="$(gh pr list --head "$current_branch" --state open --json url --jq '.[0].url // ""' 2>"$gh_err" || true)"
  if [ -n "$pr_url" ]; then
    rm -f "$marker" "$gh_err" 2>/dev/null || true
    exit 0
  fi
  if [ -s "$gh_err" ]; then
    printf 'implement-issue-gate: gh fallback failed: %s\n' "$(head -c 500 "$gh_err")" >&2
  fi
  rm -f "$gh_err" 2>/dev/null || true
fi

# Hand off red gates to precommit-gate.sh. If src/ or tests/ have uncommitted
# changes, that hook has authority — emitting our resume hint on top of its
# failure log would stack two contradictory messages.
if git diff --name-only HEAD 2>/dev/null | grep -Eq '^(src|tests)/' \
   || git diff --name-only --cached 2>/dev/null | grep -Eq '^(src|tests)/' \
   || git ls-files --others --exclude-standard 2>/dev/null | grep -Eq '^(src|tests)/'; then
  printf 'implement-issue-gate: deferring to precommit-gate (uncommitted src/tests changes)\n' >&2
  exit 0
fi

# Workflow invariant unmet — the run hasn't opened a PR and isn't blocked.
emit_resume_hint() {
  cat <<EOF
implement-issue-gate: /implement-issue run for issue #${marker_issue} on branch ${marker_branch} has not opened a PR yet — keep going, don't stop here.

  Current phase: ${marker_phase}
  Marker:        ${marker}

Resume the playbook (phase -> next step in implement-issue SKILL.md):
  - branched         -> Step 6  Implement (TaskCreate, write code + tests)
  - implemented      -> Step 6  Gate: python3 -m unittest discover -s tests
  - gates_green      -> Step 7  First commit
  - committed        -> Step 8  /code-review
  - code_reviewed    -> Step 9  Triage + fix findings
  - triaged          -> Step 10 Push the branch
  - pushed           -> Step 10 gh pr create  (write prUrl into the marker)

Legitimate stops only: write .claude/state/implement-issue-blocked.json with the
reason AND a .branch field matching '${marker_branch}' if (a) BLOCKING codex
finding you cannot resolve, (b) 3-attempt test escape clause tripped, or (c) the
branch already exists on remote. Otherwise, keep going.
EOF
}
resume_hint="$(emit_resume_hint)"

if stop_hook_additional_context_supported; then
  jq -cn --arg ctx "$resume_hint" \
    '{hookSpecificOutput: {hookEventName: "Stop", additionalContext: $ctx}}'
  exit 0
fi

printf '\n%s\n' "$resume_hint" >&2
exit 2
