#!/usr/bin/env python3
"""PreToolUse guard for Bash: enforces the project's deletion rule (CLAUDE.md).

Input: the Claude Code hook payload on stdin. Allowed: exit 0, no output. Refused:
exit 2 with the reason on stderr (exit 2 stops the call before permission rules run).

Refuses when
  * the command, or a script file it executes, reaches a remote host (ssh/scp/sftp/rsync
    to host:path) and contains a deletion or clobbering operation anywhere in its text;
  * a local rm/rmdir/unlink/shred or find -delete/-exec rm (also inside bash -c, sh -c,
    zsh -c and eval strings) names anything other than a literal absolute path inside a
    temp root (/tmp, /private/tmp, /var/folders, $TMPDIR), a variable assigned from
    mktemp into a temp root in the same text, or a literal path inside the project's
    .claude/state/ (relative paths only when the command does not cd).
Scripts executed from the trusted roots (~/.claude/scripts and <project>/.claude/scripts,
or NO_DELETE_GUARD_TRUSTED_ROOTS in the hook's environment) get only the remote check.
The project is CLAUDE_PROJECT_DIR, else the payload's cwd, else the working directory.
Fails closed: an unreadable payload or an internal error is a refusal.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
DELETE_COMMANDS = {"rm", "rmdir", "unlink", "shred", "srm"}
WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "exec", "command", "builtin", "xargs",
            "do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"}
SHELLS = {"bash", "sh", "zsh"}
DIRECTORY_CHANGES = {"cd", "pushd", "popd"}
SEPARATORS = set("\n;&|()`")
WORD_BREAKS = SEPARATORS | set(" \t")
SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".py")
SCRIPT_READ_LIMIT = 2 * 1024 * 1024
SELF = os.path.realpath(__file__)

REMOTE = re.compile(r"(?<![\w./-])(?:ssh|scp|sftp)(?![\w./-])|\brsync\b[^\n;|&]*\s[\w.@-]+:")
REMOTE_RULES = (
    ("delete command", re.compile(r"(?<![\w./-])(?:rm|rmdir|unlink|shred|srm|truncate)(?![\w./-])")),
    ("find -delete", re.compile(r"\s-delete(?![\w-])")),
    ("rsync delete option", re.compile(r"--(?:delete[\w-]*|remove-source-files)(?![\w-])")),
    ("docker removal", re.compile(r"\bdocker\s+(?:\w+\s+)?(?:rmi|prune)(?![\w-])")),
    ("python file deletion", re.compile(
        r"\bos\.(?:remove|unlink|rmdir|removedirs)\s*\(|\bshutil\.rmtree\s*\(|\.(?:unlink|rmdir)\s*\(")),
    ("overwriting redirect", re.compile(r"(?:^|[\s;|&(])>\|?(?![>&=])\s*(?!/dev/null(?![\w/.-]))\S")),
)
MOVE_OR_COPY = re.compile(r"(?<![\w./-])(mv|cp)(?=\s)")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
INTERPRETER = re.compile(r"python[\d.]*|bash|sh|zsh|source|\.")
MKTEMP_ASSIGNMENT = re.compile(r"(?<![\w$])([A-Za-z_]\w*)=([\"']?)\$\(\s*mktemp\b([^()]*)\)\2")
VARIABLE_OPERAND = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))(/.*)?")


class Context:
    def __init__(self, project: str) -> None:
        self.temp_roots = _both_forms(list(TEMP_ROOTS) + [r for r in [os.environ.get("TMPDIR", "")] if r.startswith("/")])
        self.project = os.path.normpath(project)
        self.state_roots = _both_forms([os.path.join(self.project, ".claude", "state")])
        trusted = os.environ.get("NO_DELETE_GUARD_TRUSTED_ROOTS")
        defaults = [os.path.expanduser("~/.claude/scripts"), os.path.join(self.project, ".claude", "scripts")]
        self.trusted_roots = {os.path.realpath(r) for r in (trusted.split(os.pathsep) if trusted else defaults) if r}


def _both_forms(paths: list[str]) -> set[str]:
    return {os.path.normpath(p) for p in paths} | {os.path.realpath(p) for p in paths}


def under(path: str, roots: set[str]) -> bool:
    return any(path.startswith(root + "/") for root in roots)


def inside(path: str, roots: set[str]) -> bool:
    """An absolute operand stays inside roots: globs may match the root's entries, others must be below it."""
    globs = [path.find(c) for c in "*?[" if c in path]
    if globs:
        prefix = path[:min(globs)]
        base = os.path.normpath(prefix if prefix.endswith("/") else os.path.dirname(prefix))
        return all(p in roots or under(p, roots) for p in (base, os.path.realpath(base)))
    norm = os.path.normpath(path)
    return under(norm, roots) and under(os.path.realpath(norm), roots)


def operand_ok(operand: str, ctx: Context, temp_vars: set[str], relative_ok: bool) -> bool:
    var = VARIABLE_OPERAND.fullmatch(operand)
    if var:
        rest = var.group(3) or ""
        return (var.group(1) or var.group(2)) in temp_vars and not re.search(r"[$`]|(^|/)\.\.(/|$)", rest)
    if any(c in operand for c in "$`~"):
        return False
    if operand.startswith("/"):
        return inside(operand, ctx.temp_roots) or inside(operand, ctx.state_roots)
    return relative_ok and inside(os.path.join(ctx.project, operand), ctx.state_roots)


def mktemp_in_temp(args: str, ctx: Context) -> bool:
    args = re.sub(r"\$\{TMPDIR(?::?-[^}]*)?\}|\$TMPDIR\b", "/tmp", args)
    try:
        tokens = shlex.split(args)
    except ValueError:
        return False
    tmp_flag, directory, template, i = False, None, None, 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("--directory", "--quiet", "--dry-run") or re.fullmatch(r"-[dqu]+", token):
            pass
        elif token == "-t":
            tmp_flag = True
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-") and "/" not in tokens[i + 1]:
                i += 1
        elif token == "--tmpdir":
            tmp_flag = True
        elif token.startswith("--tmpdir="):
            directory = token.split("=", 1)[1]
        elif token == "-p" and i + 1 < len(tokens):
            directory = tokens[i + 1]
            i += 1
        elif token.startswith("--suffix="):
            pass
        elif token.startswith("-") or template is not None:
            return False
        else:
            template = token
        i += 1
    if directory is not None:
        norm = os.path.normpath(directory)
        return directory.startswith("/") and (norm in ctx.temp_roots or inside(norm, ctx.temp_roots))
    if template is None or (tmp_flag and "/" not in template):
        return True
    parent = os.path.normpath(os.path.dirname(template))
    return template.startswith("/") and (parent in ctx.temp_roots or inside(parent, ctx.temp_roots))


def mktemp_variables(text: str, ctx: Context) -> set[str]:
    temp_assignments: dict[str, int] = {}
    for m in MKTEMP_ASSIGNMENT.finditer(text):
        count = temp_assignments.setdefault(m.group(1), 0)
        temp_assignments[m.group(1)] = count + 1 if mktemp_in_temp(m.group(3), ctx) else -10 ** 6
    safe = set()
    for name, count in temp_assignments.items():
        escaped = re.escape(name)
        assignments = len(re.findall(r"(?<![\w$])%s\+?=" % escaped, text))
        rebound = re.search(r"\b(?:for|read|select|local|declare|typeset)\b[^\n;]*\b%s\b(?!=)" % escaped, text)
        if count > 0 and assignments == count and not rebound:
            safe.add(name)
    return safe


def split_heredocs(text: str) -> tuple[str, list[str]]:
    """Separate heredoc bodies (data) from shell text; return (shell_text, body_lines)."""
    shell, bodies, pos = [], [], 0
    while True:
        m = HEREDOC.search(text, pos)
        if not m:
            break
        line_end = text.find("\n", m.end())
        if line_end < 0:
            break
        shell.append(text[pos:line_end + 1])
        strip_tabs = text[m.start():m.start() + 3] == "<<-"
        lines = text[line_end + 1:].split("\n")
        consumed = line_end + 1
        for i, line in enumerate(lines):
            consumed += len(line) + 1
            if (line.lstrip("\t") if strip_tabs else line) == m.group(2):
                bodies.extend(lines[:i])
                break
        else:
            bodies.extend(lines)
        pos = min(consumed, len(text))
    shell.append(text[pos:])
    return "".join(shell), bodies


def segments(text: str) -> list[str]:
    out, cur, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch == "#" and (i == 0 or text[i - 1] in WORD_BREAKS):
            end = text.find("\n", i)
            if end < 0:
                break
            i = end
            continue
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "\\" and i + 1 < len(text):
            cur.extend((ch, text[i + 1]))
            i += 2
            continue
        elif ch in SEPARATORS:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s for s in out if s.strip()]


def all_segments(text: str) -> list[str]:
    shell_text, body_lines = split_heredocs(text)
    return segments(shell_text) + [s for line in body_lines for s in segments(line)]


def words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True, posix=True)
    except ValueError:
        return segment.replace('"', " ").replace("'", " ").split()


def command_words(tokens: list[str]) -> tuple[list[str], bool]:
    i, via_xargs = 0, False
    while i < len(tokens):
        token = tokens[i]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", token):
            i += 1
            continue
        if token in WRAPPERS:
            via_xargs = via_xargs or token == "xargs"
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        break
    return tokens[i:], via_xargs


def local_problems(shell_segments: list[str], ctx: Context, temp_vars: set[str], depth: int = 0) -> list[str]:
    parsed = [command_words(words(segment)) for segment in shell_segments]
    relative_ok = not any(tokens and os.path.basename(tokens[0]) in DIRECTORY_CHANGES for tokens, _ in parsed)
    problems = []
    for tokens, via_xargs in parsed:
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        if depth < 3 and name in SHELLS and "-c" in tokens[1:-1]:
            inner = tokens[tokens.index("-c") + 1]
            problems += local_problems(all_segments(inner), ctx, temp_vars | mktemp_variables(inner, ctx), depth + 1)
        elif depth < 3 and name == "eval":
            inner = " ".join(tokens[1:])
            problems += local_problems(all_segments(inner), ctx, temp_vars | mktemp_variables(inner, ctx), depth + 1)
        elif name in DELETE_COMMANDS:
            operands, flags_done = [], False
            for token in tokens[1:]:
                if not flags_done and token == "--":
                    flags_done = True
                elif not flags_done and token.startswith("-"):
                    continue
                else:
                    operands.append(token)
            if via_xargs:
                problems.append("%s fed by xargs (paths cannot be checked)" % name)
            bad = [op for op in operands if not operand_ok(op, ctx, temp_vars, relative_ok)]
            if bad:
                problems.append("%s outside the allowed locations: %s" % (name, ", ".join(repr(b) for b in bad[:3])))
        elif name == "find":
            rest = tokens[1:]
            deleting = "-delete" in rest or any(
                t in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(rest)
                and os.path.basename(rest[i + 1]) in DELETE_COMMANDS
                for i, t in enumerate(rest))
            if deleting:
                search_roots = []
                for token in rest:
                    if token.startswith("-") or token in ("(", "!", "\\("):
                        break
                    search_roots.append(token)
                bad = [r for r in search_roots if not operand_ok(r, ctx, temp_vars, relative_ok)]
                if not search_roots or bad:
                    problems.append("find deleting outside the allowed locations: %s"
                                    % (", ".join(repr(b) for b in bad[:3]) or "current directory"))
    return problems


def remote_problems(text: str) -> list[str]:
    if not REMOTE.search(text):
        return []
    problems = []
    for label, pattern in REMOTE_RULES:
        m = pattern.search(text)
        if m:
            problems.append("%s (%r) in a command that reaches a remote host" % (label, m.group(0).strip()[:40]))
    for m in MOVE_OR_COPY.finditer(text):
        rest = re.split(r"[\n;&|)`]", text[m.end():], maxsplit=1)[0]
        flags = []
        for token in rest.split():
            if token == "--" or not token.startswith("-"):
                break
            flags.append(token)
        if not any(f in ("--no-clobber", "--update=none") or re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", f) for f in flags):
            problems.append("%s without -n (can overwrite) in a command that reaches a remote host" % m.group(1))
            break
    return problems


def executed_scripts(shell_segments: list[str]) -> list[tuple[str, str]]:
    """Script files a segment runs: the command word itself, or an interpreter's script argument."""
    found = []
    for segment in shell_segments:
        tokens, _ = command_words(words(segment))
        if not tokens:
            continue
        if INTERPRETER.fullmatch(os.path.basename(tokens[0])):
            args = tokens[1:]
            if "-c" in args or "-m" in args:
                continue
            script = next((t for t in args if not t.startswith("-")), None)
        else:
            script = tokens[0]
        if not script or not script.endswith(SCRIPT_SUFFIXES):
            continue
        path = os.path.expanduser(os.path.expandvars(script))
        if os.path.isfile(path) and os.path.realpath(path) != SELF:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                found.append((path, handle.read(SCRIPT_READ_LIMIT)))
    return found


def evaluate(command: str, project: str) -> list[str]:
    ctx = Context(project)
    shell_segments = all_segments(command)
    problems = local_problems(shell_segments, ctx, mktemp_variables(command, ctx)) + remote_problems(command)
    for path, text in executed_scripts(shell_segments):
        found = remote_problems(text)
        trusted = under(os.path.realpath(path), ctx.trusted_roots)
        if not trusted and not path.endswith(".py"):
            found += local_problems(all_segments(text), ctx, mktemp_variables(text, ctx))
        problems += ["%s (in %s)" % (p, os.path.basename(path)) for p in found]
    return problems


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        tool = payload.get("tool_name")
        if tool is not None and tool != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            raise ValueError("tool_input.command is missing")
        cwd = payload.get("cwd")
        project = os.environ.get("CLAUDE_PROJECT_DIR") or (cwd if isinstance(cwd, str) and cwd else os.getcwd())
        problems = evaluate(command, project)
    except Exception as exc:  # fail closed
        problems = ["the deletion guard could not evaluate this command (%s)" % type(exc).__name__]
    if not problems:
        return 0
    sys.stderr.write(
        "Blocked by the project deletion rule (CLAUDE.md, 'Deletion rule'): " + "; ".join(problems[:5])
        + ". On the Unraid server, delete only through the owning app's API (Sonarr/Radarr, qBittorrent, Plex). "
          "Locally, rm/find -delete may only name literal paths inside /tmp, /private/tmp, /var/folders, $TMPDIR "
          "or this project's .claude/state/, or a variable assigned from mktemp into a temp folder in the same "
          "command. Keep local temp cleanup in a separate command from anything that uses ssh.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
