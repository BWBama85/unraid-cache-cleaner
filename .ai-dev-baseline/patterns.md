# Pattern ledger

**What this project has already learned from its own review threads.** Every entry below was a
review finding somebody fixed: the class of defect, where it was found, and the commit that closed
it. It is written automatically by `/resolve-pr-threads` as each thread is resolved, and read
automatically by `/implement-issue` — the gap-analysis dispatch and the pre-PR self-review sweep
both receive the promoted checklist.

**The checklist is the operative half.** A class seen more than once is a pattern rather than an
incident, and is owed a rule: a sweep to run before the next pull request opens. Rules land here
through the normal pull-request path, so a rule only takes effect once a change carrying it has
been merged — which takes repository write access, and is reviewable in the diff like any other
change. (Write access is what the guarantee actually rests on; whether a human read the diff is up
to the project's own review settings.)

**Editing by hand is fine.** Reword a rule that reads badly, delete one that stopped being true.
The only lines with a machine-read grammar are the ones between the markers below; the prose
around them is yours.

**Resolving a `fix` sha after the pull request merged.** On a squash-merging repo the per-thread
commits never become ancestors of the default branch, so a bare `git show <fix>` fails once the
branch is gone. GitHub keeps the pull request's own commits, so fetch them by PR number — which is
why every entry carries one:

```sh
git fetch origin "refs/pull/<pr>/head" && git show <fix>
```

**Two branches can both append here, and that is handled by ordinary means.** Git may report a
conflict when two pull requests add hits at the same point — take both sides; the entries are
independent and are keyed on their review-thread ids, so nothing is lost by keeping them. Promotion
is decided by *reading this file*, never by a counter carried in a branch: two branches that each
recorded a class's first hit merge into a file holding two, and the class is then due.

**What makes that converge is a check on the CLEAN-PASS path**, not the ordinary one. A resolver run
that finds nothing to fix exits before it would ever ask which classes are due, so "the next run
promotes it" was only true of a run that happened to have other findings. `/resolve-pr-threads`
therefore reconciles due promotions before exiting on a clean pass — the one thing a clean run still
does.

## Promoted checklist

Sweep each of these before opening a pull request.

<!-- adb:checklist:begin -->
- `path-qualified-command-match` — When matching a command name in text or tokens, compare its basename and accept a leading directory path; add a regression case that invokes the command by absolute path.
- `script-path-resolution` — Resolve every executed script the way the shell will: expand variables, honour earlier directory changes, ignore the file-name suffix, and refuse when the path cannot be resolved; test a relative-after-cd case and a suffix-less case.
- `wrapper-hides-command` — Treat any wrapper (sudo, env, xargs, timeout, find -exec, a shell with -c) as hiding the real command: unwrap it fully or refuse, never assume the token after the wrapper is the command; test each wrapper with an option that takes a value.
- `combined-flag-parsing` — Never test a flag by equality: single-letter options combine (-lc, -xc, -Bc) and long options abbreviate, so match flag clusters by the letters they contain and long options by prefix; add a combined-form case for every flag the code branches on.
- `quoted-substitution-parsing` — Match command names on text with quote characters and backslashes removed as well as on the raw text, because the shell rejoins r""m and r\m; test each quoted spelling of a name the code matches.
- `redirect-form-coverage` — Cover every redirect spelling when detecting writes: attached to the previous word, with a file descriptor number, with an ampersand, appending, and force-clobber; add a case per spelling rather than assuming whitespace separates the operator.
<!-- adb:checklist:end -->

## Hits

One line per resolved review thread, newest last.

<!-- adb:hits:begin -->
- `path-qualified-command-match` `.claude/scripts/no-delete-guard.py:42` `1ba581b` `PRRT_kwDOSJrgbc6hoOC-` PR #124 2026-09-11 — Remote client given by full path was not recognised as remote
- `wrapper-hides-command` `.claude/scripts/no-delete-guard.py:268` `1ba581b` `PRRT_kwDOSJrgbc6hoODD` PR #124 2026-09-11 — Wrapper option values were mistaken for the wrapped command word
- `redirect-form-coverage` `.claude/scripts/no-delete-guard.py:50` `1ba581b` `PRRT_kwDOSJrgbc6hoODH` PR #124 2026-09-11 — Redirect with an explicit file descriptor escaped the overwrite pattern
- `script-path-resolution` `.claude/scripts/no-delete-guard.py:366` `1ba581b` `PRRT_kwDOSJrgbc6hoODM` PR #124 2026-09-11 — Relative script resolved against the wrong directory after cd
- `script-path-resolution` `.claude/scripts/no-delete-guard.py:364` `1ba581b` `PRRT_kwDOSJrgbc6hoODO` PR #124 2026-09-11 — Interpreter scripts without a known suffix were never read
- `execution-order-ignored` `.claude/scripts/no-delete-guard.py:150` `1ba581b` `PRRT_kwDOSJrgbc6hoODS` PR #124 2026-09-11 — Safe-variable proof ignored statement order and conditional execution
- `path-qualified-command-match` `.claude/scripts/no-delete-guard.py:44` `1ba581b` `PRRT_kwDOSJrgbc6hoODc` PR #124 2026-09-11 — Deletion command given by full path slipped past the remote word match
- `combined-flag-parsing` `.claude/scripts/no-delete-guard.py:284` `1ba581b` `PRRT_kwDOSJrgbc6hoODi` PR #124 2026-09-11 — Shell -c combined with other flags was not recognised
- `wrapper-hides-command` `.claude/scripts/no-delete-guard.py:314` `1ba581b` `PRRT_kwDOSJrgbc6hoODm` PR #124 2026-09-11 — find -exec action behind a wrapper was not examined
- `unchecked-action-operands` `.claude/scripts/no-delete-guard.py:321` `1ba581b` `PRRT_kwDOSJrgbc6hoODt` PR #124 2026-09-11 — find -exec action operands were never validated, only search roots
- `option-tolerant-subcommand-match` `.claude/scripts/no-delete-guard.py:47` `1ba581b` `PRRT_kwDOSJrgbc6hoODw` PR #124 2026-09-11 — docker global options defeated the prune subcommand match
- `quoted-substitution-parsing` `.claude/scripts/no-delete-guard.py:215` `1ba581b` `PRRT_kwDOSJrgbc6hoOD3` PR #124 2026-09-11 — Command substitutions inside double quotes were treated as inert text
- `redirect-form-coverage` `.claude/scripts/no-delete-guard.py:63` `761725c` `PRRT_kwDOSJrgbc6hpabt` PR #124 2026-09-11 — Redirect attached to the preceding word escaped the overwrite pattern
- `shell-expansion-escape` `.claude/scripts/no-delete-guard.py:109` `761725c` `PRRT_kwDOSJrgbc6hpabz` PR #124 2026-09-11 — Brace expansion let an operand leave the allowed roots after expansion
- `wrapper-hides-command` `.claude/scripts/no-delete-guard.py:390` `761725c` `PRRT_kwDOSJrgbc6hpab3` PR #124 2026-09-11 — find action running an ordinary script never had that script inspected
- `script-path-resolution` `.claude/scripts/no-delete-guard.py:410` `761725c` `PRRT_kwDOSJrgbc6hpab5` PR #124 2026-09-11 — Interpreter option values were mistaken for the script name
- `script-path-resolution` `.claude/scripts/no-delete-guard.py:426` `761725c` `PRRT_kwDOSJrgbc6hpacA` PR #124 2026-09-11 — A script created later in the same command was treated as inspected
- `combined-flag-parsing` `.claude/scripts/no-delete-guard.py:466` `761725c` `PRRT_kwDOSJrgbc6hpacF` PR #124 2026-09-11 — Combined Python flags such as -Bc skipped the code check
- `partial-inspection-treated-complete` `.claude/scripts/no-delete-guard.py:428` `761725c` `PRRT_kwDOSJrgbc6hpacK` PR #124 2026-09-11 — A script past the read limit was treated as fully inspected
- `guard-tamper-surface` `.claude/settings.json:52` `761725c` `PRRT_kwDOSJrgbc6hpacQ` PR #124 2026-09-11 — Approval rules covered edit tools only, leaving shell writes to guard files open
- `option-abbreviation` `.claude/scripts/no-delete-guard.py:60` `761725c` `PRRT_kwDOSJrgbc6hpacV` PR #124 2026-09-11 — Abbreviated long options defeated an exact option match
- `quoted-substitution-parsing` `.claude/scripts/no-delete-guard.py:500` `761725c` `PRRT_kwDOSJrgbc6hpacc` PR #124 2026-09-11 — Quote characters split an executable name that the remote shell rejoins
- `script-path-resolution` `.claude/scripts/no-delete-guard.py:440` `761725c` `PRRT_kwDOSJrgbc6hpach` PR #124 2026-09-11 — File name overrode the interpreter when classifying a script
<!-- adb:hits:end -->
