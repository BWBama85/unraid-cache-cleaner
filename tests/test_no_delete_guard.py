"""Guards the no-delete PreToolUse hook: its decisions and its wiring in .claude/settings.json."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".claude" / "scripts" / "no-delete-guard.py"
SETTINGS = ROOT / ".claude" / "settings.json"
HOST = "root@nas.local"
REMOTE_DELETE = "ssh %s 'rm -f /mnt/user/x'\n" % HOST
GUARD_FILE_RULES = (
    "Edit(/.claude/scripts/no-delete-guard.py)",
    "Edit(/tests/test_no_delete_guard.py)",
    "Edit(/.claude/settings.json)",
)


def run_guard(data: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k != "NO_DELETE_GUARD_TRUSTED_ROOTS"}
    base.update({"TMPDIR": "/private/var/folders/zz/guardtest/T/", "CLAUDE_PROJECT_DIR": str(ROOT)})
    base.update(env or {})
    return subprocess.run([sys.executable, str(GUARD)], input=data, capture_output=True, text=True,
                          timeout=30, env=base)


def decide(command: str | None = None, tool_name: str = "Bash", raw: str | None = None,
           env: dict[str, str] | None = None) -> str:
    data = raw if raw is not None else json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = run_guard(data, env)
    if proc.returncode == 2 and proc.stderr.strip() and not proc.stdout.strip():
        return "deny"
    if proc.returncode == 0 and not proc.stdout.strip() and not proc.stderr.strip():
        return "allow"
    raise AssertionError("unexpected guard result: exit %s, stdout %r, stderr %r"
                         % (proc.returncode, proc.stdout, proc.stderr))


DENY = [
    "ssh -i key %s 'D=/mnt/applications/appdata/transcode/claude_subs; rm -rf -- \"$D\"'" % HOST,
    "ssh %s 'bash -s' <<'EOF'\nset -u\nrm -f \"$TMP_H\"\nEOF" % HOST,
    "ssh %s 'del() { rm -- \"$1\"; }; del /mnt/disk8/x.mkv.orig-bak'" % HOST,
    "ssh %s \"find /mnt/user/data -name '*.bak' -delete\"" % HOST,
    "ssh %s 'rmdir /mnt/user/data/empty'" % HOST,
    "ssh %s docker rmi linuxserver/ffmpeg" % HOST,
    "ssh %s docker image prune -f" % HOST,
    "ssh %s 'mv -- /mnt/disk9/a.mkv /mnt/disk9/b.mkv'" % HOST,
    "ssh %s 'cp /mnt/applications/x.mkv /mnt/disk9/y.mkv'" % HOST,
    "ssh %s 'cat > /mnt/applications/appdata/manifest.tsv'" % HOST,
    "ssh %s \"python3 -c 'import os; os.remove(\\\"/mnt/user/x\\\")'\"" % HOST,
    "rsync -av --delete /tmp/out/ %s:/mnt/user/data/out/" % HOST,
    "scp /tmp/x %s:/boot/ && rm -rf /tmp/x" % HOST,
    "T=$(mktemp); ssh %s \"rm -f $T\"" % HOST,
    "rm -f \"$K\"",
    "rm -rf ~/Library/Caches/whisper",
    "rm -rf /Volumes/External/Code/unraid-cache-cleaner/build",
    "rm -rf /tmp",
    "rm -rf /tmp/../Users/brentwilson",
    "cd /tmp && rm -rf leftovers",
    "find /Volumes/External -name '*.pyc' -delete",
    "find . -exec rm {} +",
    "echo ok; sudo rm -rf /etc/hosts",
    "ls /tmp/x | xargs rm",
    "cat > /tmp/notes.txt <<'EOF'\ndon't worry\nEOF\nrm -rf /Users/brentwilson/Documents",
    "bash -s <<'EOF'\nrm -rf /Users/brentwilson/Documents\nEOF",
    "bash -c 'rm -rf /Users/brentwilson/Documents'",
    "sh -c \"cd / && rm -rf Users\"",
    "eval \"rm -rf /Users/brentwilson/Documents\"",
    "# the header says \"it does not\necho hi; rm -rf /Users/brentwilson/Documents",
    "# don't\necho hi; rm -rf /Users/brentwilson/Documents",
    "ls  # it's fine\necho hi; rm -rf /Users/brentwilson/Documents",
    "BODY=\"$(mktemp -p /Users/brentwilson)\"; rm -f \"$BODY\"",
    "D=\"$(mktemp -d scratch.XXXXXX)\"; rm -rf \"$D\"",
    "BODY=\"$(mktemp \"${TMPDIR:-/tmp}/x.XXXXXX\")\"; BODY=/Users/brentwilson/notes; rm -f \"$BODY\"",
    "D=\"$(mktemp -d)\"; rm -rf \"$D/../..\"",
    "for BODY in /Users/brentwilson/*; do :; done; BODY=$(mktemp); rm -f \"$BODY\"",
    "rm -rf .claude/state/../src",
    "rm -rf .claude/state",
    "rm -rf .claude",
    "rm -f .claude/state/$NAME",
    "cd /Users/brentwilson && rm -f .claude/state/x.json",
]

ALLOW = [
    "ls -la /tmp",
    "rm -f /tmp/aci_audio/*.wav /tmp/aci_audio/*.err",
    "rm -r /tmp/subcmp /tmp/subcmp13",
    "rm -rf /private/tmp/claude-501/scratch",
    "rm -rf /private/var/folders/zz/guardtest/T/tmpabc",
    "find /tmp/claude_subs -name '*.srt' -delete",
    "bash -c 'rm -f /tmp/scratch.txt'",
    "BODY=\"$(mktemp \"${TMPDIR:-/tmp}/issue-body.XXXXXX\")\" || exit 1\n"
    "gh issue create --title t --body-file \"$BODY\"; rc=$?\nrm -f \"$BODY\"",
    "D=$(mktemp -d); rm -rf \"$D\"",
    "T=$(mktemp -t guard); rm -f \"$T\"",
    "D=\"$(mktemp -d /tmp/work.XXXXXX)\"; rm -rf \"${D}/partial\"",
    "rm -f .claude/state/implement-issue-active.json .claude/state/implement-issue-blocked.json",
    "rm -f .claude/state/.marker.tmp",
    "rm -f %s/.claude/state/x.json" % ROOT,
    "# a comment with a \"stray quote\nls -la",
    "ssh %s 'ls /mnt/user/data | grep -c orig-bak'" % HOST,
    "ssh %s \"docker ps --format '{{.Names}}'\"" % HOST,
    "ssh %s 'mv -n /mnt/disk9/a.mkv /mnt/disk9/b.mkv'" % HOST,
    "ssh %s 'cp -n /mnt/disk9/a.mkv /mnt/disk9/b.mkv'" % HOST,
    "ssh %s \"awk -v d=1 'BEGIN{exit !(d>2)}'\"" % HOST,
    "ssh %s 'docker exec plex ffprobe x 2>/dev/null >/dev/null'" % HOST,
    "ssh-keygen -F nas.local",
    "grep -rn 'rm -rf' docs/",
    "echo \"x; rm -rf /\"",
    "python3 -m unittest discover -s tests -v",
    "python3 - <<'EOF'\nimport os\nos.remove('/tmp/a')\nEOF",
]


class GuardDecisionTests(unittest.TestCase):
    def test_denies_deletions(self) -> None:
        for command in DENY:
            with self.subTest(command=command):
                self.assertEqual(decide(command), "deny")

    def test_allows_safe_commands(self) -> None:
        for command in ALLOW:
            with self.subTest(command=command):
                self.assertEqual(decide(command), "allow")

    def test_executed_script_files_are_inspected(self) -> None:
        env = {"NO_DELETE_GUARD_TRUSTED_ROOTS": "/nonexistent-trusted-root"}
        with tempfile.TemporaryDirectory() as tmp:
            remote_sh = Path(tmp, "cleanup.sh")
            remote_sh.write_text(REMOTE_DELETE)
            remote_py = Path(tmp, "cleanup.py")
            remote_py.write_text("import os, subprocess\nsubprocess.run(['ssh', 'h', 'ls'])\nos.remove('/tmp/x')\n")
            local_py = Path(tmp, "local.py")
            local_py.write_text("import os\nos.remove('/tmp/x')\n")
            local_sh = Path(tmp, "local.sh")
            local_sh.write_text("# don't\necho start; rm -rf /Users/brentwilson/Documents\n")
            self.assertEqual(decide("bash %s" % remote_sh, env=env), "deny")
            self.assertEqual(decide(str(remote_sh), env=env), "deny")
            self.assertEqual(decide("python3 %s" % remote_py, env=env), "deny")
            self.assertEqual(decide("python3 -u %s" % remote_py, env=env), "deny")
            self.assertEqual(decide("bash %s" % local_sh, env=env), "deny")
            self.assertEqual(decide("python3 %s" % local_py, env=env), "allow")

    def test_home_relative_script_paths_are_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home:
            Path(fake_home, "cleanup.sh").write_text(REMOTE_DELETE)
            env = {"HOME": fake_home, "NO_DELETE_GUARD_TRUSTED_ROOTS": "/nonexistent-trusted-root"}
            for command in ('bash "$HOME/cleanup.sh"', 'bash "${HOME}/cleanup.sh"', "bash ~/cleanup.sh"):
                with self.subTest(command=command):
                    self.assertEqual(decide(command, env=env), "deny")

    def test_trusted_script_roots_only_skip_the_local_check(self) -> None:
        with tempfile.TemporaryDirectory() as trusted, tempfile.TemporaryDirectory() as other:
            env = {"NO_DELETE_GUARD_TRUSTED_ROOTS": trusted}
            local_cleanup = 'tmp="$1"\nrm -f "$tmp"\n'
            cases = (
                (trusted, "helper.sh", local_cleanup, "allow"),
                (trusted, "remote.sh", REMOTE_DELETE, "deny"),
                (other, "helper.sh", local_cleanup, "deny"),
            )
            for root, name, body, expected in cases:
                script = Path(root, name)
                script.write_text(body)
                with self.subTest(script=str(script)):
                    self.assertEqual(decide("bash %s" % script, env=env), expected)

    def test_files_that_are_only_arguments_are_not_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            noisy = Path(tmp, "noisy.py")
            noisy.write_text("NOTE = 'ssh host rm -rf /mnt/user'\n")
            self.assertEqual(decide("rm -f %s" % noisy), "allow")
            self.assertEqual(decide("grep -n ssh %s" % noisy), "allow")
            self.assertEqual(decide("printf '{}' | %s" % GUARD), "allow")
            self.assertEqual(decide("python3 %s" % noisy), "deny")

    def test_refusal_exits_2_with_reason_on_stderr(self) -> None:
        proc = run_guard(json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /Users/x"}}))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Deletion rule", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_fails_closed_on_bad_payload(self) -> None:
        self.assertEqual(decide(raw="not json"), "deny")
        self.assertEqual(decide(raw=json.dumps({"tool_name": "Bash", "tool_input": {}})), "deny")

    def test_ignores_other_tools(self) -> None:
        self.assertEqual(decide("rm -rf /", tool_name="Read"), "allow")


class GuardWiringTests(unittest.TestCase):
    def test_settings_run_guard_before_every_bash_call(self) -> None:
        settings = json.loads(SETTINGS.read_text())
        entries = [e for e in settings.get("hooks", {}).get("PreToolUse", []) if e.get("matcher") == "Bash"]
        commands = [h.get("command") for e in entries for h in e.get("hooks", [])]
        self.assertIn(".claude/scripts/no-delete-guard.py", commands)

    def test_guard_files_require_approval_to_edit(self) -> None:
        ask = json.loads(SETTINGS.read_text()).get("permissions", {}).get("ask", [])
        for rule in GUARD_FILE_RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, ask)

    def test_guard_is_executable(self) -> None:
        self.assertTrue(os.access(GUARD, os.X_OK))


if __name__ == "__main__":
    unittest.main()
