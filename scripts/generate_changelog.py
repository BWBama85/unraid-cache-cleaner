#!/usr/bin/env python3
"""Deterministic CHANGELOG section generator (stdlib-only).

Opt-in helper for the ``/release`` skill (issue #29). Given a git range it
groups ``git log <range>`` by conventional-commit type into the ``### Changes``
block and drafts a first-pass ``### Highlights`` from the feature/breaking
commits, emitting a section that matches the exact headings and insertion
contract the release-note extraction in ``.claude/skills/release/SKILL.md``
depends on (``## [X.Y.Z] - DATE`` / ``### Highlights`` / ``### Changes``).

The authored-highlights path in ``/release`` remains the default — the human
narrative is the value-add. This runs only when the operator opts in with
``--auto-changelog``; the output is a *draft* meant to be reviewed before the
release commit.

Stdlib-only and Python 3.9-compatible (matching ``pyproject.toml``); no
third-party changelog engine. Output is byte-identical for byte-identical
input: the category order is fixed, within-category order follows ``git log``,
and every commit is preserved exactly once (unrecognized types and
non-conventional subjects fall into ``Other``).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

# Unit separator keeps subjects containing any punctuation intact between fields.
_FIELD_SEP = "\x1f"
_LOG_FORMAT = "%h" + _FIELD_SEP + "%s"

# Conventional-commit subject: ``type(scope)!: description``.
_SUBJECT_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)"
    r"(?:\((?P<scope>[^)]*)\))?"
    r"(?P<breaking>!)?"
    r":[ \t]+(?P<desc>.+)$"
)

# Fixed category order → heading. A commit whose type is absent here (and every
# non-conventional subject) lands in ``Other`` so nothing is silently dropped.
_CATEGORIES: Tuple[Tuple[str, str], ...] = (
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactors"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("build", "Build"),
    ("ci", "CI"),
    ("chore", "Chores"),
)
_KNOWN_TYPES = frozenset(ctype for ctype, _ in _CATEGORIES)
_OTHER_HEADING = "Other"
_BREAKING_HEADING = "⚠ BREAKING CHANGES"
_HIGHLIGHTS_NOTE = (
    "<!-- First-pass draft — replace with an authored narrative before release. -->"
)


@dataclass(frozen=True)
class Commit:
    """One parsed commit. ``type`` is ``None`` for a non-conventional subject."""

    sha: str
    type: Optional[str]
    scope: Optional[str]
    breaking: bool
    description: str
    subject: str


def parse_commit(sha: str, subject: str) -> Commit:
    match = _SUBJECT_RE.match(subject)
    if match is None:
        return Commit(
            sha=sha,
            type=None,
            scope=None,
            breaking=False,
            description=subject,
            subject=subject,
        )
    return Commit(
        sha=sha,
        type=match.group("type").lower(),
        scope=match.group("scope"),
        breaking=bool(match.group("breaking")),
        description=match.group("desc"),
        subject=subject,
    )


def parse_log(lines: Iterable[str]) -> List[Commit]:
    """Parse ``git log --pretty=%h<sep>%s`` lines into commits, in log order."""
    commits: List[Commit] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        sha, sep, subject = line.partition(_FIELD_SEP)
        if not sep:  # no separator: treat the whole line as the subject
            sha, subject = "", line
        commits.append(parse_commit(sha, subject.strip()))
    return commits


def group_by_category(commits: Sequence[Commit]) -> List[Tuple[str, List[Commit]]]:
    """Return ``(heading, commits)`` groups in fixed order, each commit once.

    Breaking changes are collected into a single leading group; the remaining
    commits fall under their type's heading, then ``Other`` for anything left.
    """
    groups: List[Tuple[str, List[Commit]]] = []
    breaking = [c for c in commits if c.breaking]
    if breaking:
        groups.append((_BREAKING_HEADING, breaking))
    for ctype, heading in _CATEGORIES:
        bucket = [c for c in commits if c.type == ctype and not c.breaking]
        if bucket:
            groups.append((heading, bucket))
    other = [
        c
        for c in commits
        if not c.breaking and (c.type is None or c.type not in _KNOWN_TYPES)
    ]
    if other:
        groups.append((_OTHER_HEADING, other))
    return groups


def _bullet(commit: Commit) -> str:
    scope = "**{0}:** ".format(commit.scope) if commit.scope else ""
    sha = " ({0})".format(commit.sha) if commit.sha else ""
    return "- {0}{1}{2}".format(scope, commit.description, sha)


def render_changes(commits: Sequence[Commit]) -> str:
    groups = group_by_category(commits)
    lines: List[str] = ["### Changes", ""]
    if not groups:
        lines.append("_No changes in this range._")
        return "\n".join(lines)
    for heading, bucket in groups:
        lines.append("#### {0}".format(heading))
        lines.append("")
        lines.extend(_bullet(c) for c in bucket)
        lines.append("")
    return "\n".join(lines).rstrip()


def render_highlights(commits: Sequence[Commit]) -> str:
    notable = [c for c in commits if c.breaking or c.type == "feat"]
    lines: List[str] = ["### Highlights", "", _HIGHLIGHTS_NOTE, ""]
    if not notable:
        lines.append(
            "- _No user-facing features in this range; summarize the fixes above._"
        )
    else:
        for commit in notable:
            prefix = "**BREAKING** " if commit.breaking else ""
            scope = "{0}: ".format(commit.scope) if commit.scope else ""
            lines.append("- {0}{1}{2}".format(prefix, scope, commit.description))
    return "\n".join(lines).rstrip()


def render_section(version: str, date: str, commits: Sequence[Commit]) -> str:
    """Render the full ``## [X.Y.Z] - DATE`` section (newline-terminated)."""
    numeric = version[1:] if version.startswith("v") else version
    blocks = [
        "## [{0}] - {1}".format(numeric, date),
        render_highlights(commits),
        render_changes(commits),
    ]
    return "\n\n".join(blocks) + "\n"


def git_log(range_spec: Optional[str]) -> List[str]:
    cmd = ["git", "log", "--no-merges", "--pretty=format:" + _LOG_FORMAT]
    if range_spec:
        cmd.append(range_spec)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.splitlines()


def default_range() -> Optional[str]:
    """``<latest release tag>..HEAD``, or ``None`` (full history) if untagged."""
    result = subprocess.run(
        ["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*", "--sort=-v:refname"],
        check=True,
        capture_output=True,
        text=True,
    )
    tags = [t for t in result.stdout.splitlines() if t.strip()]
    if not tags:
        return None
    return "{0}..HEAD".format(tags[0])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic CHANGELOG section from git history."
    )
    parser.add_argument(
        "--version", required=True, help="Release version, e.g. v1.2.0 or 1.2.0."
    )
    parser.add_argument(
        "--range",
        dest="range_spec",
        default=None,
        help="git log range (e.g. v1.0.0..HEAD). "
        "Default: latest tag..HEAD, or full history if untagged.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Release date YYYY-MM-DD. Default: today (UTC).",
    )
    args = parser.parse_args(argv)

    range_spec = args.range_spec if args.range_spec is not None else default_range()
    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    commits = parse_log(git_log(range_spec))
    sys.stdout.write(render_section(args.version, date, commits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
