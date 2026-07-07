---
name: release
description: Cut a versioned release of unraid-cache-cleaner. Bumps the version, updates CHANGELOG.md, commits + tags vX.Y.Z on main, pushes, creates the GitHub Release, and surfaces the GHCR publish run.
argument-hint: '[patch|minor|major] | --version vX.Y.Z | --dry-run [patch|minor|major]'
allowed-tools: Bash, Read, Write, Edit
user-invocable: true
---

# /release

Cut a versioned release of `unraid-cache-cleaner` end-to-end and surface the resulting GHCR publish run.

> **Bespoke, not ported.** getrich's `release` skill drives `git-cliff` + `cliff.toml` + `scripts/release.mjs` + a deploy workflow — none of which exist here. This is a from-scratch rewrite: **stdlib-only, `gh`/`git` directly**, no changelog engine, no deploy step. Only the portable ideas were kept (preflight, release-goal gate, authored notes discipline, milestone roll, fix-forward safety).

**$ARGUMENTS** — bump level (`patch` | `minor` | `major`), or `--version vX.Y.Z` for an explicit version, or `--dry-run [bump]` to preview without committing/pushing. Default: `patch`. The **first** release ignores the bump arg and is always **`v1.0.0`** (owner-locked decision).

## What a release is here

- The **git tag is the single source of truth.** The GHCR image, the GitHub Release body, and the `CHANGELOG.md` section all derive from the commit the tag points to.
- `.github/workflows/publish.yml` is triggered by `push: tags: v*` — it builds and pushes `ghcr.io/bwbama85/unraid-cache-cleaner:vX.Y.Z`. It also pushes `:latest` on any `main` push. **It does NOT create a GitHub Release** — this skill does that itself with `gh release create`.
- **Two publish runs fire per cut.** Pushing the release commit to `main` triggers a `:latest` build, and pushing the tag triggers the versioned build. Step 8 surfaces the **tag** run specifically — never a blind `gh run list --limit 1`, which may show the `main`/`:latest` run instead.
- The version lives in **two** files that bump together: `pyproject.toml` (`version = "..."`) and `src/unraid_cache_cleaner/__init__.py` (`__version__ = "..."`). Every client's `User-Agent` derives from `__version__` via the `USER_AGENT` constant (`__init__.py`), so there is no third string to bump. Bump both or ship a mismatch (guarded by `tests/test_version.py`).

## Parse the invocation

Resolve `$ARGUMENTS` up front so the goal gate (dry-run skip) and version compute below can consume the results. macOS bash 3.2-safe; the `setopt` guard makes the `$ARGUMENTS` word-split under zsh too (a no-op under bash, which splits natively):

```bash
setopt sh_word_split 2>/dev/null || true    # zsh: split $ARGUMENTS on spaces like bash
LEVEL="patch"; EXPLICIT_VERSION=""; DRY_RUN=0; _next_ver=""
for arg in $ARGUMENTS; do
  case "$arg" in
    patch|minor|major) LEVEL="$arg" ;;
    --dry-run)         DRY_RUN=1 ;;
    --version)         _next_ver=1 ;;         # value is the next token
    --version=*)       EXPLICIT_VERSION="${arg#--version=}" ;;
    v[0-9]*)           [ -n "$_next_ver" ] && { EXPLICIT_VERSION="$arg"; _next_ver=""; } ;;
  esac
done
# Reject a malformed explicit version before it can ever become a tag: publish.yml
# only builds the versioned image for tags matching v*, so `1.2.0` or `v1.2` would
# create a Release with no versioned GHCR image.
if [ -n "$EXPLICIT_VERSION" ] && ! printf '%s' "$EXPLICIT_VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "ERROR: --version must be vX.Y.Z (got '$EXPLICIT_VERSION')"; exit 1
fi
echo "level=$LEVEL explicit=${EXPLICIT_VERSION:-none} dry_run=$DRY_RUN"
```

## Preflight — abort with zero side effects on any failure

Run these before touching anything. Export the Homebrew prefix first if `gh` is not on PATH (non-interactive shells).

```bash
if ! command -v gh >/dev/null 2>&1; then export PATH="/opt/homebrew/bin:$PATH"; fi

# 1. On main.
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "ERROR: not on main"; exit 1; }
# 2. Clean tree.
[ -z "$(git status --porcelain)" ] || { echo "ERROR: working tree not clean"; exit 1; }
# 3. main in sync with origin/main.
git fetch origin main --tags --quiet
[ "$(git rev-list --left-right --count HEAD...origin/main)" = "0	0" ] || { echo "ERROR: main diverges from origin/main"; exit 1; }
# 4. Latest CI run on HEAD is completed + success (not just local tests).
HEAD_SHA="$(git rev-parse HEAD)"
CI_CONCLUSION="$(gh run list --workflow=ci.yml --branch main --limit 20 \
  --json headSha,status,conclusion \
  --jq "[.[] | select(.headSha==\"$HEAD_SHA\")] | first | .conclusion // \"missing\"")"
[ "$CI_CONCLUSION" = "success" ] || { echo "ERROR: CI on HEAD is '$CI_CONCLUSION' (need success)"; exit 1; }
```

The tag-does-not-already-exist check lives in **Compute the version** below, because it needs `$VERSION` — but it still runs before anything mutating, so the zero-side-effects promise holds.

Ensure the `release-blocker` label exists so the goal gate below can query it (idempotent — a pre-existing label makes this a harmless no-op):

```bash
gh label create release-blocker --color B60205 \
  --description "Must land before the current Next release milestone can be cut" 2>/dev/null || true
```

## Release-goal gate — before bumping anything

Release goals live in the rolling **`Next release`** milestone + the **`release-blocker`** label (see `CLAUDE.md` → "Release goals"). Enforce it here so an abort has zero side effects:

```bash
if [ "$DRY_RUN" != 1 ]; then
  BLOCKERS="$(gh issue list --milestone "Next release" --label release-blocker --state open \
    --json number,title --jq '.[] | "#\(.number) \(.title)"')"
  if [ -n "$BLOCKERS" ]; then
    echo "ERROR: open release-blocker issues in Next release — cut is blocked:"; echo "$BLOCKERS"; exit 1
  fi
fi
```

The gate **hard-fails** (`exit 1`) when any blocker is open — a `gh issue list` that merely prints could be walked past unnoticed, defeating the zero-side-effect abort. Those issues are declared must-land-before-release; finish them, drop the `release-blocker` label, or move them out of `Next release` before cutting. There is no override flag — a release is deliberate and interactive — so the sole exception is the owner explicitly saying "release anyway" **in-session**, in which case skip this gate and continue. A `--dry-run` **skips this gate** (it ships nothing).

## Compute the version

Uses `LEVEL` / `EXPLICIT_VERSION` from **Parse the invocation**. `--version vX.Y.Z` overrides the bump level; otherwise bump the newest tag (or `v1.0.0` on the first cut). The tag-existence guard runs here, once `$VERSION` is known but still before any file is touched.

```bash
if [ -n "$EXPLICIT_VERSION" ]; then
  VERSION="$EXPLICIT_VERSION"
else
  # Newest existing release tag (vMAJOR.MINOR.PATCH only).
  LATEST="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -1)"
  if [ -z "$LATEST" ]; then
    VERSION="v1.0.0"                       # owner-locked first cut
  else
    core="${LATEST#v}"
    IFS=. read -r MA MI PA <<EOF
$core
EOF
    case "$LEVEL" in
      major) MA=$((MA + 1)); MI=0; PA=0 ;;
      minor) MI=$((MI + 1)); PA=0 ;;
      *)     PA=$((PA + 1)) ;;
    esac
    VERSION="v${MA}.${MI}.${PA}"
  fi
fi
NUMERIC="${VERSION#v}"
echo "target: $VERSION"

# Tag must not already exist — locally or on origin.
git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null && { echo "ERROR: tag $VERSION already exists locally"; exit 1; }
git ls-remote --exit-code --tags origin "$VERSION" >/dev/null 2>&1 && { echo "ERROR: tag $VERSION already exists on origin"; exit 1; }
```

## CHANGELOG.md — authored, stdlib-only (no git-cliff)

Prepend a new section to `CHANGELOG.md` (create the file on the first cut). Format:

```
## [X.Y.Z] - YYYY-MM-DD

### Highlights

<authored narrative — what this release does and why it matters>

### Changes

- <commit subject> (<short-sha>)
- ...
```

**Gather the window first — don't write from memory.** For the first cut there is no previous tag, so summarize the project to date from merged PRs; afterwards the range is `<previous tag>..HEAD`:

```bash
PREV="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -1)"
RANGE="${PREV:+$PREV..}HEAD"
git log $RANGE --no-merges --pretty='- %s (%h)'    # the raw commit list
gh pr list --state merged --base main --limit 60 --json number,title \
  --jq '.[] | "#\(.number) \(.title)"'             # PR titles carry more intent
TODAY="$(date -u +%Y-%m-%d)"
```

**Author `### Highlights`** (the value this skill adds over a bare commit dump): open with one sentence naming the theme of the cut, group by subsystem (qBittorrent cleanup, Plex duplicates, `*arr` layer, infra/docs), lead with operator impact, cite PR/issue numbers inline. Scale length to the release — v1.0.0 earns a few themed paragraphs; a patch earns two sentences. Put the `git log $RANGE` output under `### Changes`.

Insert the section with the Write/Edit tools **directly below the `<!-- release sections are inserted below this line, newest first -->` marker** in `CHANGELOG.md` (i.e. under the `# Changelog` preamble, above any prior version section) so newest stays first and the header preamble is preserved.

On a `--dry-run`, write the section but do not commit — inspect `git diff`, then discard or re-run for real.

## Bump the version

Update **both** sites to `$NUMERIC` (the `USER_AGENT` and all three clients follow automatically):

- `pyproject.toml` → `version = "X.Y.Z"`
- `src/unraid_cache_cleaner/__init__.py` → `__version__ = "X.Y.Z"`

Then prove the tree is consistent before committing:

```bash
python3 -m compileall -q src && python3 -m unittest tests.test_version -v
```

`tests/test_version.py` asserts `pyproject == __init__` and that every client's `User-Agent` is `unraid-cache-cleaner/{__version__}` — a mismatch fails here, before the tag exists.

## Commit, tag, push

The release commit is the **one sanctioned exception** to `CLAUDE.md`'s never-commit-`main` rule. No destructive git, ever (`CLAUDE.md`).

```bash
# A --dry-run must stop here — the blocks above only wrote the CHANGELOG + version
# bumps; nothing is committed, tagged, pushed, or released on a preview.
[ "$DRY_RUN" = 1 ] && { echo "dry-run: stopping before commit/tag/push — inspect 'git diff', then 'git checkout -- .'"; exit 0; }

git add pyproject.toml src/unraid_cache_cleaner/__init__.py CHANGELOG.md
git commit -m "chore(release): $VERSION" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git tag -a "$VERSION" -m "$VERSION"
# One atomic transaction so main and the tag advance together: a non-atomic pair
# could leave main bumped + :latest published while the versioned tag/image never
# lands. main push → :latest build; tag push → versioned image.
git push --atomic origin main "$VERSION"
```

The second `-m` supplies the `Co-Authored-By` trailer `CLAUDE.md` requires on every commit.

The push will prompt for confirmation (it is not on the settings allow-list, by design — a release is the point to have a human in the loop). Never `--force`, never `--no-verify`. The read-only preflight and publish-run inspection commands (`gh run list/view/watch`, `gh release list/view`, `git ls-remote`, `gh label list/create`) *are* allow-listed in `.claude/settings.json`, so the only steps that prompt are the three consequential writes: the annotated `git tag`, this push, and `gh release create`.

## Create the GitHub Release

`publish.yml` does not create a Release — do it here, with the just-written CHANGELOG section as the body (one source of truth). Extract this version's section with awk:

```bash
awk -v ver="[$NUMERIC]" '
  $0 ~ "^## " && index($0, ver) {grab=1; print; next}
  grab && /^## \[/ {exit}
  grab {print}
' CHANGELOG.md > /tmp/release-notes-$NUMERIC.md

gh release create "$VERSION" --title "$VERSION" --notes-file "/tmp/release-notes-$NUMERIC.md"
```

## Surface the publish run (the tag build, not `:latest`)

Isolate the **tag-triggered** run so the owner watches the versioned image build, not the `main`/`:latest` run that fired from the release commit:

```bash
sleep 5   # let the run register
RUN_ID="$(gh run list --workflow=publish.yml --branch "$VERSION" --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
if [ -n "$RUN_ID" ]; then  # // empty yields "" (not the string "null") when the run hasn't registered yet → fallback path
  gh run watch "$RUN_ID" || gh run view "$RUN_ID" --json url --jq .url
else
  gh run list --workflow=publish.yml --limit 3 --json url,displayTitle,headBranch  # fallback: print recent runs
fi
```

## Roll the release milestone — best-effort, after the tag is pushed

The tag is the source of truth; a milestone hiccup must never fail a release that already shipped. Turn the shipped `Next release` bucket into the version's record and open a fresh one:

```bash
MS="$(gh api repos/:owner/:repo/milestones --jq '.[] | select(.title=="Next release") | .number')"
if [ -n "$MS" ]; then
  gh api --method PATCH repos/:owner/:repo/milestones/"$MS" -f title="$VERSION"
  gh api repos/:owner/:repo/milestones -f title="Next release" -f state=open
  for n in $(gh issue list --milestone "$VERSION" --state open --limit 100 --json number --jq '.[].number'); do
    gh issue edit "$n" --milestone "Next release"
  done
  gh api --method PATCH repos/:owner/:repo/milestones/"$MS" -f state=closed
fi
```

Skip this entirely on a `--dry-run`.

## When to use which bump

- `patch` (default) — bug fixes, doc tweaks, internal refactors. No operator-visible behavior change.
- `minor` — new subcommand, new env var, new report surface. Backward compatible.
- `major` — a breaking change: a removed/renamed env var, changed report schema, or anything that forces the operator to reconfigure.

## Dry runs

```
/release --dry-run minor
```

Runs preflight (minus the goal gate), computes the version, writes the CHANGELOG section and the version bumps, then **stops** — nothing is committed, tagged, pushed, or released. Inspect `git diff`, then `git checkout -- .` to discard, or re-run without `--dry-run` to ship.

## Failure surface

- **"not on main" / "working tree not clean" / "main diverges"** — fix the git state; never `--no-verify` past it.
- **"CI on HEAD is not success"** — do not release a red (or un-tested) commit. Fix the run and re-merge first. `"missing"` means HEAD predates a CI run — push a trivial commit or wait for CI, don't skip the check.
- **"tag already exists"** — that version already shipped; pick the next bump.
- **Release-goal gate prints blockers** — finish them, drop the `release-blocker` label, or move them out of `Next release` before cutting (or get explicit owner "release anyway").
- **Publish workflow fails mid-build** — do **not** delete the tag (that rewrites history and orphans the GitHub Release). Investigate the run, fix forward, cut the next patch.
- **`gh run watch` unavailable** — fall back to the printed run URL.
