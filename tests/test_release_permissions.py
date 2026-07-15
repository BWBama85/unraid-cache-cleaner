"""Regression guard for the /release human-checkpoint hardening (issue #51).

The three consequential release writes — the annotated ``git tag``, the ``git
push`` to ``main``, and ``gh release create`` — must stay behind tracked
``permissions.ask`` rules in ``.claude/settings.json``. ``ask`` overrides any
``allow`` in every mode (including ``bypassPermissions``) regardless of scope,
so these rules are what keeps the checkpoint from being silently defeated by a
broad ``settings.local.json`` grant or the skill's own bare-``Bash``
``allowed-tools`` (see the caveat in ``.claude/skills/release/SKILL.md``).

These are structural string checks — there is no permission-engine or shell
parser in this stdlib-only repo, so they model Claude Code's ``Bash(prefix:*)``
prefix semantics (command equals the prefix, or begins with the prefix + a
space) to prove two things: every release write matches an ask rule, and none
of the read-only preflight/inspection commands the release skill also runs do.
They do NOT prove Claude Code's real runtime precedence (that is the manual
verification noted in the issue); they DO fail loudly if a rule is dropped or a
release write is reworded out from under its rule.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"
_RELEASE_SKILL = _REPO_ROOT / ".claude" / "skills" / "release" / "SKILL.md"

#: The exact ask rules issue #51 (and the SKILL.md caveat) prescribe.
_EXPECTED_ASK_RULES = (
    "Bash(git push:*)",
    "Bash(git tag -a:*)",
    "Bash(gh release create:*)",
)


def _prefix_of(rule: str) -> str:
    """The command prefix a ``Bash(<prefix>:*)`` rule matches on."""

    assert rule.startswith("Bash(") and rule.endswith(":*)"), rule
    return rule[len("Bash(") : -len(":*)")]


def _matches(rule: str, command: str) -> bool:
    """Model Claude Code's ``Bash(prefix:*)`` prefix match for a bare command."""

    prefix = _prefix_of(rule)
    return command == prefix or command.startswith(prefix + " ")


class ReleaseAskRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
        self.ask_rules = self.settings.get("permissions", {}).get("ask", [])

    def test_the_three_ask_rules_are_present(self) -> None:
        for rule in _EXPECTED_ASK_RULES:
            self.assertIn(rule, self.ask_rules, f"missing release ask rule: {rule}")

    def test_ask_rules_are_valid_bash_prefix_patterns(self) -> None:
        # A malformed pattern (e.g. a missing ``:*``) would silently never match and
        # re-open the hole, so pin the shape.
        for rule in self.ask_rules:
            self.assertTrue(
                rule.startswith("Bash(") and rule.endswith(":*)"),
                f"ask rule is not a Bash(prefix:*) pattern: {rule}",
            )

    def test_each_release_write_is_caught_by_an_ask_rule(self) -> None:
        # The realized commands the release skill runs (VERSION substituted).
        writes = {
            "Bash(git tag -a:*)": 'git tag -a "v1.2.3" -m "v1.2.3"',
            "Bash(git push:*)": 'git push --atomic origin main "v1.2.3"',
            "Bash(gh release create:*)": (
                'gh release create "v1.2.3" --title "v1.2.3" '
                '--notes-file "/tmp/release-notes-1.2.3.md"'
            ),
        }
        for rule, command in writes.items():
            self.assertIn(rule, self.ask_rules)
            self.assertTrue(_matches(rule, command), f"{rule!r} should catch {command!r}")

    def test_annotated_tag_variants_are_all_gated(self) -> None:
        # #51 review (codex): the annotated-tag checkpoint must survive equivalent spellings
        # of the SAME tag creation. The skill runs as prompt text (not a fixed script), so
        # `git tag --annotate`, `-m`/`--message` (both imply -a), `-am`, and the signed forms
        # must all prompt — otherwise a reworded tag command dodges the checkpoint.
        variants = [
            'git tag -a "v1.2.3" -m "v1.2.3"',
            'git tag --annotate "v1.2.3" -m "v1.2.3"',
            'git tag -am "v1.2.3" "v1.2.3"',
            'git tag -m "v1.2.3" "v1.2.3"',
            'git tag --message "v1.2.3" "v1.2.3"',
            'git tag -s "v1.2.3" -m "v1.2.3"',
            'git tag --sign "v1.2.3" -m "v1.2.3"',
        ]
        for command in variants:
            self.assertTrue(
                any(_matches(rule, command) for rule in self.ask_rules),
                f"no ask rule gates annotated-tag creation: {command!r}",
            )

    def test_read_only_release_commands_are_not_over_prompted(self) -> None:
        # The preflight/inspection commands allow-listed for the release skill (#28) must
        # keep running unprompted — no ask rule may catch them. Includes the read-only
        # `git tag` query forms that sit next to the gated annotated-creation flags.
        read_only = [
            "git tag",
            "git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname",
            "git tag -l 'v*'",
            "git tag -n5",
            "git tag --sort=-v:refname",
            "git tag --merged HEAD",
            "git tag --contains HEAD",
            "git ls-remote --tags origin",
            "git fetch origin main",
            "gh release list",
            "gh release view v1.2.3",
            "gh run list --limit 5",
            "gh run view 123456",
            "gh run watch 123456",
            "gh label list",
            "gh label create release-blocker --color FF0000",
        ]
        for command in read_only:
            for rule in self.ask_rules:
                self.assertFalse(
                    _matches(rule, command),
                    f"ask rule {rule!r} would over-prompt read-only {command!r}",
                )

    def test_skill_still_runs_the_writes_the_rules_target(self) -> None:
        # Anchor the rules to reality: if the skill rewords a write out from under its
        # ask rule, this fails so the rules get revisited rather than silently bypassed.
        skill = _RELEASE_SKILL.read_text(encoding="utf-8")
        for template in (
            'git tag -a "$VERSION"',
            "git push --atomic origin main",
            'gh release create "$VERSION"',
        ):
            self.assertIn(template, skill, f"release skill no longer runs: {template}")


if __name__ == "__main__":
    unittest.main()
