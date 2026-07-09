"""Tests for the opt-in changelog generator (``scripts/generate_changelog.py``).

The generator is stdlib-only and must be deterministic: byte-identical input
yields byte-identical output, every commit is preserved exactly once, and the
section headings match the extraction contract in the ``/release`` skill.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_changelog as gc  # noqa: E402


def _line(sha: str, subject: str) -> str:
    return sha + gc._FIELD_SEP + subject


class ParseCommitTests(unittest.TestCase):
    def test_unscoped_feat(self) -> None:
        commit = gc.parse_commit("abc1234", "feat: add plex report")
        self.assertEqual(commit.type, "feat")
        self.assertIsNone(commit.scope)
        self.assertFalse(commit.breaking)
        self.assertEqual(commit.description, "add plex report")

    def test_scoped_fix(self) -> None:
        commit = gc.parse_commit("abc1234", "fix(plex): stop dropping mismatches")
        self.assertEqual(commit.type, "fix")
        self.assertEqual(commit.scope, "plex")
        self.assertEqual(commit.description, "stop dropping mismatches")

    def test_breaking_bang_unscoped(self) -> None:
        commit = gc.parse_commit("abc1234", "feat!: drop legacy env var")
        self.assertEqual(commit.type, "feat")
        self.assertTrue(commit.breaking)
        self.assertEqual(commit.description, "drop legacy env var")

    def test_breaking_bang_scoped(self) -> None:
        commit = gc.parse_commit("abc1234", "refactor(config)!: rename WATCH_PATHS")
        self.assertEqual(commit.type, "refactor")
        self.assertEqual(commit.scope, "config")
        self.assertTrue(commit.breaking)

    def test_type_is_lowercased(self) -> None:
        self.assertEqual(gc.parse_commit("a", "FEAT: shout").type, "feat")

    def test_non_conventional_subject(self) -> None:
        commit = gc.parse_commit("abc1234", "address /code-review feedback (#20)")
        self.assertIsNone(commit.type)
        self.assertIsNone(commit.scope)
        self.assertFalse(commit.breaking)
        # The full subject is preserved as the human-facing description.
        self.assertEqual(commit.description, "address /code-review feedback (#20)")

    def test_unknown_type_kept(self) -> None:
        commit = gc.parse_commit("abc1234", "wip: experiment")
        self.assertEqual(commit.type, "wip")

    def test_unicode_subject_preserved(self) -> None:
        commit = gc.parse_commit("abc1234", "docs: café ☕ notes")
        self.assertEqual(commit.description, "café ☕ notes")


class ParseLogTests(unittest.TestCase):
    def test_missing_separator_treated_as_subject(self) -> None:
        commits = gc.parse_log(["just a bare subject with no sep"])
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].sha, "")
        self.assertEqual(commits[0].subject, "just a bare subject with no sep")

    def test_blank_lines_skipped(self) -> None:
        commits = gc.parse_log(["", "  ", _line("a1", "feat: x"), ""])
        self.assertEqual(len(commits), 1)

    def test_log_order_preserved(self) -> None:
        commits = gc.parse_log(
            [_line("a1", "feat: first"), _line("b2", "fix: second")]
        )
        self.assertEqual([c.sha for c in commits], ["a1", "b2"])


class GroupTests(unittest.TestCase):
    def _commits(self):
        return gc.parse_log(
            [
                _line("a1", "feat: alpha"),
                _line("b2", "fix: beta"),
                _line("c3", "feat!: gamma"),
                _line("d4", "wip: delta"),
                _line("e5", "address bot review"),
                _line("f6", "feat: epsilon"),
            ]
        )

    def test_every_commit_preserved_exactly_once(self) -> None:
        commits = self._commits()
        grouped = gc.group_by_category(commits)
        emitted = [c.sha for _, bucket in grouped for c in bucket]
        self.assertEqual(sorted(emitted), sorted(c.sha for c in commits))
        self.assertEqual(len(emitted), len(commits))  # no duplicates

    def test_breaking_group_leads(self) -> None:
        grouped = gc.group_by_category(self._commits())
        self.assertEqual(grouped[0][0], gc._BREAKING_HEADING)
        self.assertEqual([c.sha for c in grouped[0][1]], ["c3"])

    def test_breaking_excluded_from_type_group(self) -> None:
        grouped = dict(gc.group_by_category(self._commits()))
        feature_shas = [c.sha for c in grouped["Features"]]
        self.assertIn("a1", feature_shas)
        self.assertIn("f6", feature_shas)
        self.assertNotIn("c3", feature_shas)  # the breaking feat is not double-listed

    def test_other_catches_unknown_and_non_conventional(self) -> None:
        grouped = dict(gc.group_by_category(self._commits()))
        other_shas = [c.sha for c in grouped[gc._OTHER_HEADING]]
        self.assertEqual(sorted(other_shas), ["d4", "e5"])

    def test_fixed_category_order(self) -> None:
        commits = gc.parse_log(
            [_line("a", "chore: z"), _line("b", "fix: y"), _line("c", "feat: x")]
        )
        headings = [h for h, _ in gc.group_by_category(commits)]
        self.assertEqual(headings, ["Features", "Bug Fixes", "Chores"])


class RenderTests(unittest.TestCase):
    def _section(self) -> str:
        commits = gc.parse_log(
            [
                _line("a1", "feat(plex): add report"),
                _line("b2", "fix: stop crash"),
                _line("c3", "chore!: drop py38"),
                _line("d4", "address review"),
            ]
        )
        return gc.render_section("v1.2.0", "2026-07-09", commits)

    def test_section_has_required_headings(self) -> None:
        section = self._section()
        self.assertIn("## [1.2.0] - 2026-07-09", section)
        self.assertIn("### Highlights", section)
        self.assertIn("### Changes", section)

    def test_version_prefix_v_stripped(self) -> None:
        self.assertTrue(gc.render_section("v9.9.9", "2026-01-01", []).startswith(
            "## [9.9.9] - 2026-01-01"
        ))

    def test_headings_safe_for_release_note_extractor(self) -> None:
        # The /release awk extractor grabs from the first `^## ` line and stops
        # at the next `^## [`. Nothing inside a section may start with `## `.
        lines = self._section().splitlines()
        version_headers = [ln for ln in lines if re.match(r"^## ", ln)]
        self.assertEqual(version_headers, ["## [1.2.0] - 2026-07-09"])

    def test_bullet_carries_scope_and_sha(self) -> None:
        section = self._section()
        self.assertIn("- **plex:** add report (a1)", section)

    def test_bullet_without_scope(self) -> None:
        section = self._section()
        self.assertIn("- stop crash (b2)", section)

    def test_highlights_lists_feat_and_breaking(self) -> None:
        highlights = gc.render_highlights(
            gc.parse_log(
                [
                    _line("a1", "feat: shiny"),
                    _line("b2", "fix: dull"),
                    _line("c3", "perf!: fast but breaking"),
                ]
            )
        )
        self.assertIn("- shiny", highlights)
        self.assertIn("- **BREAKING** fast but breaking", highlights)
        self.assertNotIn("dull", highlights)  # plain fixes are not highlights

    def test_highlights_empty_when_no_features(self) -> None:
        highlights = gc.render_highlights(gc.parse_log([_line("a", "fix: only")]))
        self.assertIn("No user-facing features", highlights)

    def test_empty_range_renders_placeholder(self) -> None:
        section = gc.render_section("v1.0.1", "2026-07-09", [])
        self.assertIn("_No changes in this range._", section)
        self.assertIn("## [1.0.1] - 2026-07-09", section)

    def test_deterministic_byte_identical(self) -> None:
        self.assertEqual(self._section(), self._section())

    def test_section_ends_with_single_newline(self) -> None:
        section = self._section()
        self.assertTrue(section.endswith("\n"))
        self.assertFalse(section.endswith("\n\n"))


class SkillWiringTests(unittest.TestCase):
    """The /release skill must reference the helper and gate it behind opt-in."""

    _SKILL = (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "skills"
        / "release"
        / "SKILL.md"
    )

    def setUp(self) -> None:
        self.text = self._SKILL.read_text(encoding="utf-8")

    def test_references_helper_script(self) -> None:
        self.assertIn("scripts/generate_changelog.py", self.text)

    def test_documents_opt_in_flag(self) -> None:
        self.assertIn("--auto-changelog", self.text)

    def test_automation_is_gated_not_default(self) -> None:
        # The helper is only invoked when AUTO_CHANGELOG=1, so the authored path
        # (the default) never triggers it.
        self.assertIn("AUTO_CHANGELOG=0", self.text)  # default off
        self.assertIn("AUTO_CHANGELOG=1", self.text)  # opt-in gate


class _GitRepo:
    """Minimal throwaway git repo for the integration tests."""

    def __init__(self, root: str) -> None:
        self.root = root

    def run(self, *args: str) -> str:
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout

    def commit(self, subject: str) -> None:
        self.run("commit", "--allow-empty", "--no-gpg-sign", "-m", subject)


class GitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _GitRepo(self._tmp.name)
        self.repo.run("init", "-q", "-b", "main")
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def test_git_log_excludes_merges_and_honors_range(self) -> None:
        self.repo.commit("feat: base")
        self.repo.run("tag", "v1.0.0")
        self.repo.run("switch", "-q", "-c", "feature")
        self.repo.commit("feat: on branch")
        self.repo.run("switch", "-q", "main")
        self.repo.commit("fix: on main")
        self.repo.run("merge", "--no-ff", "--no-gpg-sign", "-m", "merge: bring it in", "feature")

        commits = gc.parse_log(gc.git_log("v1.0.0..HEAD"))
        subjects = [c.subject for c in commits]
        self.assertIn("feat: on branch", subjects)
        self.assertIn("fix: on main", subjects)
        self.assertNotIn("feat: base", subjects)  # excluded by the range
        self.assertNotIn("merge: bring it in", subjects)  # excluded by --no-merges

    def test_default_range_untagged_is_none(self) -> None:
        self.repo.commit("feat: only")
        self.assertIsNone(gc.default_range())

    def test_default_range_uses_latest_tag(self) -> None:
        self.repo.commit("feat: one")
        self.repo.run("tag", "v1.0.0")
        self.repo.commit("feat: two")
        self.assertEqual(gc.default_range(), "v1.0.0..HEAD")


if __name__ == "__main__":
    unittest.main()
