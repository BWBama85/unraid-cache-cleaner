---
name: resolve-pr-threads
description: Resolve unresolved bot-authored review threads on an open PR. Switches to the PR's head branch, addresses findings (commit + push if needed), replies, then marks each thread Resolved via GraphQL so branch protection unblocks merge.
argument-hint: <pr-number>
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, TaskCreate, TaskUpdate, TaskList
user-invocable: true
---

# /resolve-pr-threads

Address and resolve every unresolved **bot-authored** review thread on PR **#$ARGUMENTS** so the repo's "all comments must be resolved" branch protection releases.

> **Port note.** Python port of the getrich skill — GraphQL machinery unchanged; the gate is `python3 -m unittest discover -s tests` instead of `pnpm`.

> **Side effect:** this skill `git switch`-es your working tree to the PR's head branch. Finish or stash unrelated work first. It aborts on a dirty tree but will not warn before switching on a clean tree.

## When to invoke

- **After `/implement-issue` exits** with bot reviews not yet posted (the documented resume path).
- **Any time** Codex, Copilot, or another configured bot reviewer posts findings. Idempotent — safe to re-run.

## Scope

**In scope:** unresolved review threads whose first comment was authored by a known automated reviewer login:

- `chatgpt-codex-connector` (OpenAI Codex — note: **no** `[bot]` suffix, so it must be matched by explicit login)
- `gemini-code-assist[bot]`, `gemini-code-assist`
- `copilot-pull-request-reviewer[bot]`, `copilot[bot]`
- `github-actions[bot]`, `claude[bot]`, `claude-code[bot]`

**Out of scope:** human-authored threads (never auto-resolve — they need human-to-human discussion; report and skip). Also out of scope: opening/merging PRs, requesting re-review, anything beyond addressing + resolving the listed threads.

## Steps

### 1. Preflight

```bash
PR_NUM="$(printf -- '%s' "$ARGUMENTS" | awk '{print $1}')"
[ -z "$PR_NUM" ] && { echo "ERROR: no PR number"; exit 1; }

if ! command -v gh >/dev/null 2>&1; then export PATH="/opt/homebrew/bin:$PATH"; fi
command -v gh || { echo "MISSING:gh"; exit 1; }
command -v jq || { echo "MISSING:jq"; exit 1; }

PR_META=$(gh pr view "$PR_NUM" --json state,headRefName,baseRefName,url 2>/dev/null) || {
  echo "ERROR: PR #$PR_NUM not found or no access"; exit 1; }
PR_STATE=$(echo "$PR_META" | jq -r .state)
PR_BRANCH=$(echo "$PR_META" | jq -r .headRefName)
[ "$PR_STATE" = "OPEN" ] || { echo "ERROR: PR #$PR_NUM is $PR_STATE"; exit 1; }

[ -z "$(git status --porcelain)" ] || { echo "ERROR: working tree dirty; commit or stash first"; exit 1; }

git fetch origin "$PR_BRANCH" --quiet || true
git switch "$PR_BRANCH" 2>/dev/null || git switch -c "$PR_BRANCH" "origin/$PR_BRANCH"
```

### 2. Fetch unresolved threads

```bash
OWNER=$(gh repo view --json owner -q .owner.login)
REPO=$(gh repo view --json name -q .name)
mkdir -p .claude/state

gh api graphql -f query='
query($owner:String!,$repo:String!,$num:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$num){
      reviewThreads(first:50){
        nodes{ id isResolved isOutdated
          comments(first:5){ nodes{ id author{login} path line body createdAt } } }
      }
    }
  }
}' -f owner="$OWNER" -f repo="$REPO" -F num="$PR_NUM" > .claude/state/threads-$PR_NUM.json

TOTAL=$(jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false)] | length' .claude/state/threads-$PR_NUM.json)
if [ "${TOTAL:-0}" -ge 50 ]; then
  echo "ERROR: $TOTAL unresolved threads — pagination not implemented. Triage manually."; exit 1
fi
```

### 3. Classify each thread

Read `.claude/state/threads-$PR_NUM.json` and decide per unresolved thread:

| Disposition | Criteria | Action |
| --- | --- | --- |
| **Legitimate code change** | Real bug/correctness issue you agree with | Edit, run the gate, commit, push, reply + resolve |
| **Already addressed** | A prior commit in this PR fixed it | Reply `Addressed in <sha>: <summary>`, then resolve |
| **Disagree with reason** | Claim is wrong / doesn't apply / declined style pref | Reply `Declined: <reason>`, then resolve |
| **Human-authored** | Login not in the known-bot list | Skip; log in summary |

### 4. Address legitimate findings

1. Make the change (Edit/Write).
2. Run the gate — never push red:
   ```bash
   python3 -m compileall -q src tests && python3 -m unittest discover -s tests
   ```
3. Commit (`address bot review on PR #$PR_NUM: <summary>`). Bundle tightly-related fixes; keep unrelated ones separate.

After all fixes are committed, push once:

```bash
git push origin "$PR_BRANCH"
LAST_SHA=$(git rev-parse --short HEAD)
```

### 5. Reply + resolve each thread

```bash
THREAD_ID="<id from threads-$PR_NUM.json>"
REPLY="Addressed in $LAST_SHA: <summary>."   # OR "Declined: <reason>." OR "Addressed in <earlier-sha>."

gh api graphql -f query='
mutation($threadId:ID!,$body:String!){
  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId, body:$body}){ comment{ id } }
}' -f threadId="$THREAD_ID" -f body="$REPLY"

gh api graphql -f query='
mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ id isResolved } } }' -f id="$THREAD_ID"
```

If the reply mutation fails, still attempt the resolve — branch protection checks `isResolved`, not the reply.

### 6. Verify + summary

```bash
REMAINING=$(gh api graphql -f query='
query($owner:String!,$repo:String!,$num:Int!){
  repository(owner:$owner,name:$repo){ pullRequest(number:$num){
    reviewThreads(first:50){ nodes{ isResolved comments(first:1){ nodes{ author{login} } } } } } }
}' -f owner="$OWNER" -f repo="$REPO" -F num="$PR_NUM" \
| jq '[.data.repository.pullRequest.reviewThreads.nodes[]
        | select(.isResolved==false)
        | select(.comments.nodes[0].author.login | test("(?i)\\[bot\\]$|^gemini-code-assist$|^chatgpt-codex-connector$"))] | length')
echo "Remaining unresolved bot threads on PR #$PR_NUM: $REMAINING"
```

Emit a concise summary: resolved N; fixed+committed (sha); already-addressed; declined; skipped (human); remaining (name them if >0).

## Important rules

- **Never resolve a human-authored thread.** **Never push to `main`.** **Never force-push.** **Never `--no-verify`.** **Never amend already-pushed commits** (new commit per bot-review batch).
- **Idempotent:** a second run should be a no-op; skip threads already `isResolved`.

## Failure modes

- **PR not OPEN** → abort. **Working tree dirty** → abort. **Ambiguous bot finding** → reply `Need clarification: <what>`, do not resolve. **Gates red after a fix** → revert the fix in a new commit, leave the thread unresolved with an explanation, ask the user. **≥50 threads** → abort (pagination intentionally not implemented).
