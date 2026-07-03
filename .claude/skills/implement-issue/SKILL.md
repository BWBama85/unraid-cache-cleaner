---
name: implement-issue
description: Implement a GitHub issue end-to-end with a codex gap-analysis pass and a native /code-review pass, then open a PR.
argument-hint: <issue-number> [more-issue-numbers…] [extra hints]
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, TaskCreate, TaskUpdate, TaskList, Agent, Skill
user-invocable: true
---

# /implement-issue

Implement GitHub issue(s) **#$ARGUMENTS** end-to-end. Run autonomously — only stop if genuinely blocked.

> **Port note.** This is the Python port of the getrich `/implement-issue` flow. The git/gh/marker/gate machinery is identical; the toolchain is adapted: this repo is **stdlib-only Python** with no typecheck/lint, so the quality gate is `python3 -m unittest discover -s tests` (plus `compileall`). See `CLAUDE.md` for conventions.

**Multi-issue runs.** If `$ARGUMENTS` begins with more than one issue number (whitespace- and/or comma-separated, e.g. `5, 6 7` or `5 6 7 some hints`), implement **all of them together on one shared branch and one PR**. Everything below operates over the whole set; the PR `Closes` each issue it fully resolves and `Refs` any it only slices. A single issue number behaves exactly as before.

## Continuation invariant

**A turn that ends with an `issue-NN-*` branch checked out and no open PR is a bug.**

This is enforced by `.claude/scripts/implement-issue-gate.sh` — a Stop hook that keeps the turn going until the run produces an open PR or declares itself blocked. Skill outputs (codex gap-analysis JSON in step 4, `/code-review` JSON in step 9) are **inputs to the next step, not deliverables**. If you feel tempted to end the turn after a sub-skill returns, you have just hit the failure mode this invariant exists to prevent — keep going.

## State protocol

The run is tracked in two gitignored state files under `.claude/state/`:

- **`.claude/state/implement-issue-active.json`** — marker for an in-flight run:

  ```json
  {
  	"branch": "issue-NN-slug",
  	"issue": "NN",
  	"phase": "started|branched|implemented|gates_green|committed|code_reviewed|triaged|pushed|pr_opened|complete",
  	"startedAt": "ISO-8601 UTC",
  	"prUrl": "https://github.com/.../pull/N"
  }
  ```

  Step 5 writes it (`phase=branched`) — _after_ the real branch exists, never before. Each step updates `phase`. Step 10 writes `prUrl` after `gh pr create`. The hook auto-deletes the marker once `prUrl` is set OR `phase=complete`. **Multi-issue:** `.issue` is the comma-joined list (e.g. `"5,6,7"`) and `.branch` carries every number (e.g. `issue-5-6-7-slug`).

- **`.claude/state/implement-issue-blocked.json`** — written by _you_ ONLY on a documented legitimate stop:
  - BLOCKING codex finding you cannot resolve from the codebase + CLAUDE.md.
  - 3-attempt test escape clause tripped.
  - Branch already exists on remote.

  Shape: `{"reason": "<one line>", "phase": "<phase>", "branch": "<branch>", "issue": "<num or CSV>"}`. **`branch` and `issue` are required and must match the active marker** — the gate validates them so a stale blocked file can't grant an unrelated run a free pass.

Preflight (step 1) **unconditionally clears** any pre-existing active/blocked state files.

## The flow

1. **Preflight** — verify tooling, clean git state, fresh `main`. Clear stale state files. (Marker is _not_ written here; see step 5.)
2. **Fetch issue** — read the issue body and comments.
3. **Codex gap analysis** — `codex exec` reviews the issue for ambiguities, missing acceptance criteria, hidden constraints. (Not redundant with step 8 — different model, different task.)
4. **Decide** — if codex finds a _blocking_ ambiguity you can't resolve from CLAUDE.md + the codebase, write `implement-issue-blocked.json` and stop. Otherwise capture the gaps as assumptions for the PR and proceed.
   - **Out-of-scope is never just a note.** The moment ANYTHING is declared out of scope, deferred, or "later" — by you, codex, `/code-review`, or the parent issue's own "Out of scope" section — it is owed a GitHub issue (step 12). A parent issue listing its own non-goals is **not** tracking; that list vanishes when the issue closes on merge. File it. No exceptions, no asking first.
5. **Branch** — create `issue-<num>-<slug>` off `main`. Write the active marker (`phase=branched`).
6. **Implement** — follow `CLAUDE.md` conventions strictly. Use TaskCreate to track sub-tasks. Keep diffs focused. Add/extend tests. Run the gate until green. Update `phase=implemented` → `gates_green`.
7. **First commit** — semantic message referencing the issue. Update `phase=committed`.
8. **Code review** — invoke the native `/code-review` skill at an appropriate effort. Findings stay in-session. Update `phase=code_reviewed`.
9. **Triage and fix** — fix legitimate findings; for deferred/disagreed, note the reason. Re-run the gate. Final commit if anything changed. Update `phase=triaged`.
10. **Push + open PR** — body includes the issue link, summary, codex gaps + how addressed, `/code-review` findings + dispositions, and a test plan. Update `phase=pushed` → `pr_opened`; write `prUrl`.
11. **Final close-out** — emit the self-attested completion checklist, write `phase=complete`, and emit the `/resolve-pr-threads <PR#>` resume hint. Do **not** poll for bot reviews.
12. **File GitHub issues for ALL deferred / out-of-scope work — mandatory, before `phase=complete`.**

---

## Important rules

- **Out-of-scope work always becomes a GitHub issue — automatically, before close-out.** Never ask whether to file; never leave it as a PR-body note. Single most-missed rule; non-negotiable.
- **Always close with the completion checklist** (step 11), even on a clean run with nothing deferred. Render each item's real status (✅ / ⚠️ / ❌).
- **Documentation ships with the change.** Any user/operator-facing addition/change/removal (a subcommand, env var, report field, default, behavior) MUST update its docs _in this PR_: `README.md` (overview + the Configuration table), `.env.example`, `contrib/unraid-cache-cleaner.xml` (Unraid template `Variable` entries), and/or `docs/*`. A change whose docs land "later" is incomplete. The step-11 checklist has a dedicated Documentation line — resolve it explicitly (name what changed, or "no user/operator-facing change").
- **No interactive checkpoints.** Run all the way through unless genuinely blocked. The Stop hook enforces this.
- **Skill outputs are not stopping points.** `/code-review` / codex JSON is _input to the next step_.
- **Never push to `main`.** Always a feature branch + PR.
- **Never `--no-verify`** on commits or pushes. Fix hook failures at the root.
- **Never use destructive git** (`reset --hard`, `push --force`, `clean -fd`) unless the user explicitly asked.
- **Codex output is advisory.** You are the implementer; disagree when wrong, but document why in the PR.
- **Cost discipline.** One codex gap-analysis pass (step 3), one `/code-review` pass (step 8). Don't loop either on its own output.

---

## Step-by-step playbook

### 1. Preflight

Parse the **leading issue number(s)** from `$ARGUMENTS` (bare integers, whitespace/comma separated; everything from the first non-numeric token onward is prose hints). Never interpolate `$ARGUMENTS` raw into a shell command.

```bash
read -r -a _argtokens <<< "$(printf '%s' "$ARGUMENTS" | tr ',' ' ')"
ISSUE_NUMS=()
for tok in "${_argtokens[@]}"; do
  case "$tok" in
    ''|*[!0-9]*) break ;;
    *) ISSUE_NUMS+=("$tok") ;;
  esac
done
[ "${#ISSUE_NUMS[@]}" -eq 0 ] && { echo "ERROR: no issue number"; exit 1; }
ISSUE_NUM="${ISSUE_NUMS[0]}"
ISSUE_CSV="$(IFS=,; printf '%s' "${ISSUE_NUMS[*]}")"
ISSUE_DASH="$(IFS=-; printf '%s' "${ISSUE_NUMS[*]}")"
echo "implementing issue(s): ${ISSUE_NUMS[*]}"
```

Verify tooling. `gh`/`codex` may not be on PATH in non-interactive shells; export the Homebrew prefix once if `gh` is missing.

```bash
if ! command -v gh >/dev/null 2>&1; then export PATH="/opt/homebrew/bin:$PATH"; fi
command -v gh      || { echo "MISSING:gh";      exit 1; }
command -v codex   || { echo "MISSING:codex (gap-analysis will be skipped — see failure modes)"; }
command -v python3 || { echo "MISSING:python3"; exit 1; }
```

Verify git state: clean tree, on `main`, zero divergence from `origin/main`.

```bash
[ -z "$(git status --porcelain)" ] || { echo "ERROR: working tree not clean"; exit 1; }
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || { echo "ERROR: not on main (on $branch)"; exit 1; }
git fetch origin main --quiet
divergence="$(git rev-list --left-right --count HEAD...origin/main)"
[ "$divergence" = "0	0" ] || { echo "ERROR: main diverges from origin/main: $divergence"; exit 1; }
gh auth status 2>&1 | head -3
```

Clear stale state (do **not** write the active marker yet — see step 5):

```bash
mkdir -p .claude/state
rm -f .claude/state/implement-issue-active.json .claude/state/implement-issue-blocked.json
```

Until step 5 the gate is inactive (no marker), which is fine for steps 2–4 (nothing committed).

### 2. Fetch the issue(s)

```bash
for n in "${ISSUE_NUMS[@]}"; do
  gh issue view "$n" --json number,title,body,labels,author,comments,milestone > "/tmp/issue-$n.json"
done
```

Read each with the Read tool. Note title, body, acceptance criteria, labels, **milestone** (`.milestone.title` — needed in step 12 to place follow-ups). In a multi-issue run, note relationships (shared files, ordering) and anything already shipped on `main` (verify before re-implementing).

### 3. Codex gap analysis

Render the issue(s) into one markdown payload, then pipe prompt + payload to codex as a single stdin stream. **One pass over the whole set.** Use `--cd` so codex can cross-reference `CLAUDE.md` and the code.

> **Long timeout.** `codex exec` reads and reasons over the repo — routinely **3–7 minutes**. Run this Bash call with an explicit `timeout` of **at least 420000 (7 min), up to 600000 (10 min)**. The default 2 min will SIGTERM codex mid-analysis (exit 143).

```bash
{
  for n in "${ISSUE_NUMS[@]}"; do
    jq -r '"# Issue #" + (.number|tostring) + ": " + .title + "\n\n" + .body + "\n\n## Comments\n" + ([.comments[] | "- " + .author.login + ": " + .body] | join("\n"))' \
      "/tmp/issue-$n.json"
    printf '\n\n---\n\n'
  done
} > "/tmp/issue-$ISSUE_NUM.md"

{
  cat <<'PROMPT'
You are reviewing one or more GitHub issues before implementation. When more than one issue is present they are implemented TOGETHER on one branch — so ALSO flag cross-issue interactions (shared files, ordering, conflicts) and whether any item already appears done on `main`. Read the issue(s) below and the repo at the working directory. Identify:

1. Blocking ambiguities — anything that prevents starting implementation.
2. Hidden constraints — CLAUDE.md conventions, neighboring code patterns, safety/perf requirements not stated.
3. Out-of-scope creep risk — where a naive implementation would balloon scope.
4. Test gaps — what tests are needed but not specified.

Output a short markdown report. Tag each finding BLOCKING / SHOULD-CLARIFY / NICE-TO-HAVE. Be specific. If well-scoped, say so.

--- ISSUES ---
PROMPT
  cat "/tmp/issue-$ISSUE_NUM.md"
} | codex exec --cd "$(git rev-parse --show-toplevel)" - > "/tmp/issue-$ISSUE_NUM-gaps.md"
```

Read `/tmp/issue-$ISSUE_NUM-gaps.md`.

### 4. Decide

- Any **BLOCKING** finding you can't resolve from CLAUDE.md or the codebase → summarize, ask the user, and exit cleanly (no marker written yet).
- Otherwise → continue; record SHOULD-CLARIFY items as assumptions for the PR body.

### 5. Create branch + write the active marker

Derive the slug from the **first** issue's title (lowercase, ASCII, non-alphanumerics → `-`, collapse, ~40 char cap).

```bash
SLUG="<computed from the first issue's title>"
BRANCH="issue-${ISSUE_DASH}-${SLUG}"

# Branch creation MUST succeed before the active marker is written. If the branch
# already exists, `git switch -c` fails but the checkout stays on `main`; writing
# the active marker anyway would point it at a branch you are not on, the gate's
# branch-mismatch guard would exit 0, and the rest of the playbook would run on
# `main` — violating the never-commit-`main` rule. So guard it: on failure, write
# the blocked marker and stop instead.
if ! git switch -c "$BRANCH" 2>/dev/null; then
  jq -n --arg branch "$BRANCH" --arg issue "$ISSUE_CSV" \
    '{reason:"branch already exists", phase:"branch_exists", branch:$branch, issue:$issue}' \
    > .claude/state/.blocked.tmp && mv .claude/state/.blocked.tmp .claude/state/implement-issue-blocked.json
  echo "ERROR: branch $BRANCH already exists (or checkout failed) — stopping, still on $(git rev-parse --abbrev-ref HEAD)"
  exit 1
fi

jq -n --arg branch "$BRANCH" --arg issue "$ISSUE_CSV" \
  --arg startedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{branch:$branch, issue:$issue, phase:"branched", startedAt:$startedAt}' \
  > .claude/state/.marker.tmp && mv .claude/state/.marker.tmp .claude/state/implement-issue-active.json
```

Every subsequent marker update follows the same `jq → .marker.tmp → mv` atomic pattern (never stage through `/tmp`). Do **not** force-push if the branch also exists on the remote.

### 6. Implement

- `TaskCreate` 3–8 tracked sub-tasks; mark in-progress / completed as you go.
- Read existing code before editing. Follow `CLAUDE.md`: stdlib-only (no new deps), frozen `Config` dataclass with **defaulted** new fields, `pathlib.Path`, per-module `logging.getLogger(__name__)`, custom `*ClientError(RuntimeError)`, dry-run/report-only safety.
- Write or extend tests in `tests/` (the `FakeClient` pattern in `tests/test_service.py`).
- **Update documentation in the same PR** (README + Configuration table, `.env.example`, `contrib/unraid-cache-cleaner.xml`, `docs/*`) for any user/operator-facing change. Sign this off at step 11.
- Run the gate and fix anything red before committing:

```bash
python3 -m compileall -q src tests && python3 -m unittest discover -s tests -v
```

**Escape clause:** if the _same_ check fails three consecutive times after fix attempts, write the blocked marker (include `branch` AND `issue` from the active marker) and stop:

```bash
jq -n --arg reason "3-attempt escape clause: <what kept failing>" \
      --arg branch "$(jq -r .branch .claude/state/implement-issue-active.json)" \
      --arg issue "$(jq -r .issue .claude/state/implement-issue-active.json)" \
      '{reason:$reason, phase:"escape_clause", branch:$branch, issue:$issue}' \
      > .claude/state/.blocked.tmp && mv .claude/state/.blocked.tmp .claude/state/implement-issue-blocked.json
```

Update marker phase: `implemented` once code is written, `gates_green` once the gate passes.

### 7. First commit

> **Write the real issue number(s) literally.** The heredoc is single-quoted (`<<'EOF'`) so `$ISSUE_NUM` will not expand, and shell variables don't persist across Bash calls anyway — substitute the actual number (e.g. `(#5)` / `Refs #5`), not `$ISSUE_NUM`.

```bash
git add <specific files>   # NOT `git add -A`
git commit -m "$(cat <<'EOF'
<type>(<scope>): <subject> (#<N>)

<body explaining the why; multi-issue: note which change serves which issue>

Refs #<N>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

Update marker phase to `committed`.

### 8. Native code review

```
/code-review high
```

Use `high` for changes touching `qbittorrent.py`/`plex.py`/`arr.py`, `planner.py`/`dedupe.py`, `service.py`/`state.py`, deletion or report-safety logic, or `.claude/` workflow files; `medium` elsewhere. Do **not** pass `--comment` (no PR exists yet). When it returns, continue to step 9. Update phase to `code_reviewed`.

### 9. Triage and fix

- **CRITICAL / HIGH:** fix. Always.
- **MEDIUM:** fix unless clearly out of scope (then defer with a GitHub issue in step 12).
- **LOW:** fix if cheap; else document in the PR body.
- **Disagree:** document the reasoning in the PR body — never silently ignore.

Re-run the gate; commit again if anything changed (`address /code-review feedback (#$ISSUE_NUM)`). Update phase to `triaged`.

### 10. Push and open PR

```bash
BRANCH="$(jq -r .branch .claude/state/implement-issue-active.json)"
git push -u origin "$BRANCH"
jq '.phase = "pushed"' .claude/state/implement-issue-active.json > .claude/state/.marker.tmp \
  && mv .claude/state/.marker.tmp .claude/state/implement-issue-active.json
```

One `Closes #N` line per issue this PR fully resolves; `Refs #N` for any you only sliced.

> **Write the real numbers literally** — the `--body` heredoc is single-quoted so `$ISSUE_NUM` won't expand, and the `--title` runs in a fresh shell where `$ISSUE_NUM` is unset. Substitute the actual issue number(s) everywhere below (`Closes #5`, `(#5)`).

```bash
PR_URL=$(gh pr create --title "<type>(<scope>): <subject> (#<N>)" --body "$(cat <<'EOF'
## Summary
- <bullet>

Closes #<N>

## Pre-implementation gap analysis (codex)
<SHOULD-CLARIFY items and how they were resolved as assumptions>

## /code-review findings
| Severity | Finding | Disposition |
|---|---|---|
| HIGH | ... | Fixed in <sha> |
| MEDIUM | ... | Deferred — tracked in #NNN |

## Test plan
- [ ] `python3 -m compileall -q src tests` passes
- [ ] `python3 -m unittest discover -s tests -v` passes
- [ ] <feature-specific manual/integration check>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)")

jq --arg url "$PR_URL" --arg phase "pr_opened" '.prUrl = $url | .phase = $phase' \
   .claude/state/implement-issue-active.json > .claude/state/.marker.tmp \
   && mv .claude/state/.marker.tmp .claude/state/implement-issue-active.json
```

Print the PR URL.

### 11. Final close-out

**Hard gate — BEFORE writing `phase=complete`:** run step 12 and confirm every deferred / out-of-scope item has a tracking issue. If one is missing, file it now.

Do **not** poll for bot reviews. Then:

```bash
jq '.phase = "complete"' .claude/state/implement-issue-active.json > .claude/state/.marker.tmp \
  && mv .claude/state/.marker.tmp .claude/state/implement-issue-active.json
```

Emit the **self-attested completion checklist** (render real status per item; never silently drop a skipped item):

> ## /implement-issue completion checklist — #<n>
>
> **Setup** — ✅ preflight · ✅ issue(s) fetched + milestone captured · ✅ codex gap-analysis (or skipped: reason)
> **Implementation** — ✅ branch off `main` + marker · ✅ CLAUDE.md conventions (stdlib-only · defaulted Config fields · Path · per-module loggers · *ClientError · report-only safety) · ✅ tests added · ✅ **docs updated** (name what, or "no user/operator-facing change") · ✅ gate green (compileall + unittest) · ✅ commits semantic, no `--no-verify`
> **Review** — ✅ `/code-review` ran · ✅ CRITICAL/HIGH fixed; MEDIUM/LOW/disagreements dispositioned
> **Ship** — ✅ pushed to feature branch · ✅ PR opened: `<PR_URL>` · ✅ PR body complete · ✅ `Closes #<n>` [· `Refs #<n>` if sliced]
> **Close-out** — ✅ deferred work filed & linked (or "none deferred") · ✅ marker `phase=complete` · ✅ resume hint emitted
>
> **⚠️ Needs attention** — <each non-✅ item + one-line reason, or "—">
>
> Follow-up issues filed (milestone — why): <list, or omit if none>
>
> If bot reviews land after this turn, run `/resolve-pr-threads <PR#>` — branch protection requires every thread Resolved before merge.

### 12. File GitHub issues for ALL deferred / out-of-scope work (mandatory)

**Always runs.** Anything not shipped in this PR that someone might need later lives in the issue tracker, never only in a PR body or a closed issue's prose. **Do not ask whether to file — file, then inform the user.**

File an issue for every un-tracked item in: the parent issue's own "Out of scope" / "Future" list; slices cut because the parent was too big; codex items resolved by deferring; `/code-review` findings triaged "deferred"; known test/infra gaps. Group tightly-related items under one umbrella; file independents separately. `gh issue list --state open --search ...` first to avoid duplicates.

**Milestones.** This repo uses a rolling **`Next release`** milestone, a standing **`Backlog`** milestone, and a **`release-blocker`** label (see `CLAUDE.md` → "Release goals"). Every filed issue gets exactly one milestone, chosen per-item using the parent's milestone as the starting signal:

- Parent ∈ `Next release` and this is a direct slice/dependency → `Next release` (add `--label release-blocker` if the release goal is genuinely incomplete without it).
- Tangential / speculative / opportunistic → `Backlog`, even if the parent is slated.
- Parent ∈ `Backlog` / none → `Backlog` unless it must ship in the imminent cut.

Create a milestone once if missing: `gh api repos/:owner/:repo/milestones -f title="Next release" -f state=open`.

```bash
gh issue create --title "<concise scope>" --label enhancement [--label release-blocker] \
  --milestone "<Next release|Backlog>" --body-file /tmp/issue-<slug>.md
```

Each body states goal, scope, explicit acceptance criteria, dependencies, and `Refs #<parent>`. Then link from **both** the parent issue and the PR so the link survives after the parent closes:

```bash
gh issue comment <parent-num> --body "Deferred/out-of-scope work now tracked: #<a>, #<b>."
gh pr comment <PR#> --body "Follow-up tracking issues: #<a>, #<b>."
```

Done.

---

## Failure modes

- **Codex times out in step 3** (exit 143): the Bash timeout fired, not codex — re-run with `420000`–`600000`.
- **Codex unavailable / errors:** skip the pass, note "codex gap-analysis skipped: <reason>" in the PR body. Don't block the PR on tooling.
- **`/code-review` unavailable:** fall back to `/simplify` and file a follow-up to bump the toolchain.
- **Tests won't go green after the 3-attempt escape clause:** write `implement-issue-blocked.json`, stop, report what's failing. Do not push red.
- **Hook fails on commit:** fix the root cause, new commit (never `--amend` published work, never `--no-verify`).
- **`precommit-gate.sh` keeps blocking:** fix the underlying test/compile failure.
- **`implement-issue-gate.sh` keeps blocking:** you're trying to end before the PR is open — open it, or write `implement-issue-blocked.json` with a real reason.
- **Branch already exists on remote:** write the blocked marker, stop, ask — do not force-push.
- **Bot reviews not handled in-turn:** by design — emit the `/resolve-pr-threads <PR#>` resume hint and exit.
