#!/usr/bin/env python3
"""PreToolUse guard for Bash: enforces the project's deletion rule (CLAUDE.md).

Input: the Claude Code hook payload on stdin. Allowed: exit 0, no output. Refused: exit 2 with the
reason on stderr (exit 2 stops the call before permission rules run).

Fails closed. Refuses when
  * the command, or a script it executes, reaches a remote host (ssh/scp/sftp, also by full path,
    or rsync to host:path) and contains a deletion or clobbering operation anywhere in its text,
    also after quote characters are removed;
  * a deletion word (rm, rmdir, unlink, shred, srm, also by full path, or -delete) cannot be
    verified: it must be the command word of a plain invocation, with no wrapper (sudo, env, xargs,
    ...), whose targets are literal absolute paths inside a temp root (/tmp, /private/tmp,
    /var/folders, $TMPDIR), a variable assigned from mktemp into a temp root by an earlier
    unconditional statement and never reassigned, or literal paths inside the project's
    .claude/state/ (relative only when the command does not change directory). Targets with brace
    expansion are refused. Deletion words anywhere else (quotes, $( ), backticks, arguments) are
    refused; bash/sh/zsh -c and eval strings get the same check;
  * find -exec runs a deletion outside those locations, a shell, interpreter or wrapper, or a script
    that fails these checks;
  * a Python deletion call (os.remove, shutil.rmtree, .unlink(), ...) in python -c code, a heredoc
    fed to Python or an executed Python script takes anything but a literal allowed path;
  * an executed script (interpreter argument, or a text file run by path, any name) fails these
    checks, does not exist yet, is too large to inspect, has interpreter options the guard cannot
    parse, or has a path it cannot resolve (variables left, or relative after a directory change);
  * a command writes to a guard file (.claude/settings.json, .claude/settings.local.json,
    .claude/scripts/no-delete-guard.py, tests/test_no_delete_guard.py) by redirect, a writing
    command (tee, cp, mv, sed -i, chmod, git checkout, ...) or Python file APIs.
Scripts under the trusted roots (~/.claude/scripts and <project>/.claude/scripts, or
NO_DELETE_GUARD_TRUSTED_ROOTS in the hook's environment) get only the remote check; a script is
trusted when its invoked path or its real path is inside a root. The interpreter that runs a script
decides how it is checked, not its file name. Heredoc bodies are checked only when fed to a shell or
Python (or written to a .sh/.py file); other bodies are data.
The project is CLAUDE_PROJECT_DIR, else the payload's cwd; scripts resolve against the payload's
cwd, else the working directory.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import sys

TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
DELETE_COMMANDS = {"rm", "rmdir", "unlink", "shred", "srm"}
KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"}
WRAPPERS = {"sudo", "doas", "su", "env", "nohup", "time", "exec", "command", "builtin", "xargs", "nice",
            "ionice", "timeout", "stdbuf", "chroot", "watch", "parallel", "setsid"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
DIRECTORY_CHANGES = {"cd", "pushd", "popd"}
WRITERS = {"tee", "cp", "mv", "install", "ln", "dd", "truncate", "rsync", "ditto", "patch", "chmod", "chown",
           "chflags", "xattr"}
IN_PLACE_EDITORS = {"sed", "perl", "ruby"}
GIT_OVERWRITES = {"checkout", "restore", "apply", "stash", "reset", "mv"}
SHELL_VALUE_OPTIONS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
SHELL_LONG_FLAGS = {"--login", "--noprofile", "--norc", "--posix", "--restricted", "--verbose", "--noediting",
                    "--debugger", "--help", "--version"}
PYTHON_VALUE_OPTIONS = {"-W", "-X", "--check-hash-based-pycs"}
SEPARATORS = set("\n;&|()")
WORD_BREAKS = SEPARATORS | set(" \t`")
MAX_DEPTH = 4
SCRIPT_READ_LIMIT = 2 * 1024 * 1024
SELF = os.path.realpath(__file__)

DELETE_WORD = r"(?<![\w.$-])(?:[\w.~/-]*/)?(?:rm|rmdir|unlink|shred|srm)(?![\w./-])"
EMBEDDED_DELETE = re.compile(DELETE_WORD)
PY_DELETE = re.compile(r"(?:\bos\.(?:remove|unlink|rmdir|removedirs)|\bshutil\.rmtree|\.(?:unlink|rmdir))\s*\(\s*([^),]*)")
PY_WRITE = re.compile(r"\bopen\s*\([^)]*['\"][rbt]*[wax+][rbt+]*['\"]|\.write_(?:text|bytes)\s*\("
                      r"|\bos\.(?:replace|rename|chmod)\s*\(|\bshutil\.(?:copy\w*|move)\s*\(")
PROTECTED = re.compile(r"(?:\.claude/(?:settings(?:\.local)?\.json|scripts/no-delete-guard\.py)|tests/test_no_delete_guard\.py)(?![\w.-])")
REMOTE = re.compile(r"(?<![\w.-])(?:[\w.~/-]*/)?(?:ssh|scp|sftp)(?![\w./-])|\brsync\b[^\n;|&]*\s[\w.@-]+:")
REMOTE_RULES = (
    ("delete command", re.compile(DELETE_WORD + r"|(?<![\w.-])truncate(?![\w.-])")),
    ("find -delete", re.compile(r"(?<![\w-])-delete(?![\w-])")),
    ("rsync delete option", re.compile(r"--(?:del[\w-]*|remove-s[\w-]*)(?![\w-])")),
    ("docker removal", re.compile(r"\bdocker\b[^\n;|&]*?(?<![\w-])(?:rmi|prune)(?![\w-])")),
    ("python file deletion", PY_DELETE),
    ("overwriting redirect", re.compile(r"(?<![<>=&\d-])(?:\d+|&)?>\|?(?![>&=])\s*(?!/dev/null(?![\w/.-]))[^\s>&|;]")),
)
MOVE_OR_COPY = re.compile(r"(?<![\w.-])(?:[\w.~/-]*/)?(mv|cp)(?=\s)")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
INTERPRETER = re.compile(r"python[\d.]*|bash|sh|zsh|dash|ksh|source|\.")
PYTHON = re.compile(r"python[\d.]*")
C_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*\+?=.*")
MKTEMP_SEGMENT = re.compile(r"\s*([A-Za-z_]\w*)=([\"']?)\$\(\s*mktemp\b([^()]*)\)\2\s*")
VARIABLE_OPERAND = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))(/.*)?")
REDIRECTION = re.compile(r"(?:\d+|&)?(?:>>?|<|>&|<&)(.*)")


class Context:
    def __init__(self, project: str, cwd: str) -> None:
        tmpdir = os.environ.get("TMPDIR", "")
        self.temp_roots = both_forms(list(TEMP_ROOTS) + ([tmpdir] if tmpdir.startswith("/") else []))
        self.project = os.path.normpath(project)
        self.cwd = cwd
        self.state_roots = both_forms([os.path.join(self.project, ".claude", "state")])
        trusted = os.environ.get("NO_DELETE_GUARD_TRUSTED_ROOTS")
        defaults = [os.path.expanduser("~/.claude/scripts"), os.path.join(self.project, ".claude", "scripts")]
        roots = trusted.split(os.pathsep) if trusted else defaults
        self.trusted_roots = both_forms([os.path.abspath(r) for r in roots if r])


def both_forms(paths: list[str]) -> set[str]:
    return {os.path.normpath(p) for p in paths} | {os.path.realpath(p) for p in paths}


def base(token: str) -> str:
    return os.path.basename(token.rstrip("/"))


def under(path: str, roots: set[str]) -> bool:
    return any(path.startswith(root + "/") for root in roots)


def inside(path: str, roots: set[str]) -> bool:
    """An absolute operand stays inside roots: globs may match the root's entries, others must be below it."""
    globs = [path.find(c) for c in "*?[" if c in path]
    if globs:
        prefix = path[:min(globs)]
        start = os.path.normpath(prefix if prefix.endswith("/") else os.path.dirname(prefix))
        return all(p in roots or under(p, roots) for p in (start, os.path.realpath(start)))
    norm = os.path.normpath(path)
    return under(norm, roots) and under(os.path.realpath(norm), roots)


def operand_ok(operand: str, ctx: Context, safe_vars: set[str], relative_ok: bool) -> bool:
    var = VARIABLE_OPERAND.fullmatch(operand)
    if var:
        rest = var.group(3) or ""
        return (var.group(1) or var.group(2)) in safe_vars and not re.search(r"[$`{}]|(^|/)\.\.(/|$)", rest)
    if any(c in operand for c in "$`~{}") or re.search(r"(^|/)\.\.(/|$)", operand):
        return False
    if operand.startswith("/"):
        return inside(operand, ctx.temp_roots) or inside(operand, ctx.state_roots)
    return relative_ok and inside(os.path.join(ctx.project, operand), ctx.state_roots)


def operands_of(tokens: list[str]) -> list[str]:
    found, flags_done, skip_target = [], False, False
    for token in tokens:
        if skip_target:
            skip_target = False
            continue
        redirect = REDIRECTION.fullmatch(token)
        if redirect:
            skip_target = not redirect.group(1)
            continue
        if not flags_done and token == "--":
            flags_done = True
        elif not flags_done and token.startswith("-") and token != "-":
            continue
        else:
            found.append(token)
    return found


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


def split_shell(text: str) -> list[tuple[str, str | None]]:
    """Quote-, comment- and substitution-aware split into (segment, separator before it)."""
    out: list[tuple[str, str | None]] = []
    cur: list[str] = []
    state = {"sep": None}
    quote, i = None, 0

    def flush(new_sep: str) -> None:
        if "".join(cur).strip():
            out.append(("".join(cur), state["sep"]))
            state["sep"] = new_sep
        elif state["sep"] not in ("&&", "||"):
            state["sep"] = new_sep
        cur.clear()

    while i < len(text):
        ch = text[i]
        if quote == "'":
            cur.append(ch)
            if ch == "'":
                quote = None
        elif ch == "$" and text[i + 1:i + 2] == "(":
            j, depth = i + 2, 1
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            cur.append(text[i:j])
            i = j
            continue
        elif ch == "`":
            j = text.find("`", i + 1)
            j = len(text) if j < 0 else j + 1
            cur.append(text[i:j])
            i = j
            continue
        elif quote == '"':
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
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
        elif ch in "&|" and text[i + 1:i + 2] == ch:
            flush(ch * 2)
            i += 2
            continue
        elif ch in SEPARATORS:
            flush(ch)
        else:
            cur.append(ch)
        i += 1
    flush("")
    return out


def words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.replace('"', " ").replace("'", " ").split()


def strip_prefix(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens) and (tokens[i] in KEYWORDS or ASSIGNMENT.fullmatch(tokens[i])):
        i += 1
    return tokens[i:]


def drop_redirections(tokens: list[str]) -> list[str]:
    kept, skip_target = [], False
    for token in tokens:
        if skip_target:
            skip_target = False
            continue
        redirect = REDIRECTION.fullmatch(token)
        if redirect:
            skip_target = not redirect.group(1)
            continue
        kept.append(token)
    return kept


def heredoc_consumer(prefix: str) -> str:
    """'shell', 'python' or 'data': what reads a heredoc whose operator follows prefix."""
    segs = split_shell(prefix)
    tokens = strip_prefix(words(segs[-1][0])) if segs else []
    if not tokens:
        return "data"
    names = [base(t) for t in tokens]
    if names[0] in WRAPPERS:
        if any(n in SHELLS for n in names):
            return "shell"
        return "python" if any(PYTHON.fullmatch(n) for n in names) else "data"
    args = drop_redirections(tokens[1:])
    if names[0] in SHELLS:
        if any(C_FLAG.fullmatch(a) for a in args):
            return "data"
        return "shell" if "-s" in args or all(a.startswith(("-", "+")) for a in args) else "data"
    if PYTHON.fullmatch(names[0]):
        return "python" if "-" in args or all(a.startswith("-") for a in args) else "data"
    targets = [t.lstrip(">") for t in tokens[1:]]
    if any(t.endswith((".sh", ".bash", ".zsh")) for t in targets):
        return "shell"
    return "python" if any(t.endswith(".py") for t in targets) else "data"


def heredoc_operators(text: str) -> list[tuple[int, int, str, bool]]:
    """(start, end, delimiter, strip tabs) for each heredoc operator that is not inside quotes."""
    found, quote, i = [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "\\" and i + 1 < len(text):
            i += 2
            continue
        elif ch == "<" and text[i + 1:i + 2] == "<":
            m = HEREDOC.match(text, i)
            if m:
                found.append((m.start(), m.end(), m.group(2), text[i:i + 3] == "<<-"))
                i = m.end()
                continue
        i += 1
    return found


def split_heredocs(text: str) -> tuple[str, list[str], list[str]]:
    """Return (shell text, shell heredoc bodies, Python heredoc bodies). Data bodies are dropped;
    a marker with no terminator line is not a heredoc."""
    shell, shell_bodies, python_bodies, pos = [], [], [], 0
    for start, end, delimiter, strip_tabs in heredoc_operators(text):
        if start < pos:
            continue
        line_end = text.find("\n", end)
        if line_end < 0:
            break
        shell.append(text[pos:line_end + 1])
        line_start = max(pos, text.rfind("\n", 0, start) + 1)
        kind = heredoc_consumer(text[line_start:start])
        lines = text[line_end + 1:].split("\n")
        consumed = line_end + 1
        for i, line in enumerate(lines):
            consumed += len(line) + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                body = "\n".join(lines[:i])
                if kind == "shell":
                    shell_bodies.append(body)
                elif kind == "python":
                    python_bodies.append(body)
                pos = min(consumed, len(text))
                break
        else:
            pos = line_end + 1
    shell.append(text[pos:])
    return "".join(shell), shell_bodies, python_bodies


def unverified(tokens: list[str]) -> list[str]:
    for token in tokens:
        if token == "-delete" or base(token) in DELETE_COMMANDS or EMBEDDED_DELETE.search(token):
            return ["a deletion the guard cannot verify (%r)" % token[:60]]
    return []


def python_problems(code: str, ctx: Context) -> list[str]:
    problems = []
    for m in PY_DELETE.finditer(code):
        try:
            value = ast.literal_eval(m.group(1).strip())
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            value = None
        if not isinstance(value, str) or not operand_ok(value, ctx, set(), False):
            problems.append("a Python deletion the guard cannot verify (%r)" % m.group(0)[:60])
    for line in code.splitlines():
        if PROTECTED.search(line) and PY_WRITE.search(line):
            problems.append("Python code that writes to a guard file (%r)" % line.strip()[:60])
            break
    return problems


def protected_path(token: str) -> bool:
    """A guard file by any spelling: .claude/./settings.json and .claude//scripts/... normalise to one."""
    return any(PROTECTED.search(form) for form in (token, os.path.normpath(token)))


def tamper_problems(raw: list[str]) -> list[str]:
    """Refuse writes to the guard's own files: redirects, writing commands, in-place edits, git restores."""
    if not any(protected_path(t) for t in raw):
        return []
    names = [base(t) for t in raw]
    for k, token in enumerate(raw):
        redirect = REDIRECTION.fullmatch(token)
        if redirect and ">" in token:
            target = redirect.group(1) or (raw[k + 1] if k + 1 < len(raw) else "")
            if protected_path(target):
                return ["a redirect onto a guard file (%r)" % target[:60]]
    if any(n in WRITERS for n in names):
        return ["a write to a guard file"]
    if any(n in IN_PLACE_EDITORS for n in names) and any(t.startswith("-i") or t == "--in-place" for t in raw):
        return ["an in-place edit of a guard file"]
    if "git" in names:
        after = raw[names.index("git") + 1:]
        if any(t in GIT_OVERWRITES for t in after):
            return ["a git command that can overwrite a guard file"]
    return []


def mktemp_assignments(segs: list[tuple[str, str | None]], text: str, ctx: Context) -> dict[str, int]:
    found: dict[str, list[int]] = {}
    for i, (seg, sep) in enumerate(segs):
        m = MKTEMP_SEGMENT.fullmatch(seg)
        if m and sep in (None, "\n", ";") and mktemp_in_temp(m.group(3), ctx):
            found.setdefault(m.group(1), []).append(i)
    safe = {}
    for name, where in found.items():
        escaped = re.escape(name)
        total = len(re.findall(r"(?<![\w$])%s\+?=" % escaped, text))
        rebound = re.search(r"\b(?:for|read|select|local|declare|typeset|export|unset)\b[^\n;]*\b%s\b" % escaped, text)
        if len(where) == 1 and total == 1 and not rebound:
            safe[name] = where[0]
    return safe


def find_problems(args: list[str], ctx: Context, safe: set[str], relative_ok: bool, depth: int) -> list[str]:
    roots = []
    for token in args:
        if token.startswith("-") or token in ("(", "!", "\\("):
            break
        roots.append(token)
    problems, outside, deleting, k = [], [], False, len(roots)
    while k < len(args):
        token = args[k]
        if token == "-delete":
            deleting = True
        elif token in FIND_ACTIONS:
            end = next((m for m in range(k + 1, len(args)) if args[m] in (";", "+")), len(args))
            action = args[k + 1:end]
            name = base(action[0]) if action else ""
            if name in DELETE_COMMANDS:
                deleting = True
                bad = [op for op in operands_of(action[1:]) if op != "{}" and not operand_ok(op, ctx, safe, relative_ok)]
                if bad:
                    problems.append("find %s deletes outside the allowed locations: %s" % (token, ", ".join(repr(b) for b in bad[:3])))
            elif not name or name in SHELLS or name in WRAPPERS or PYTHON.fullmatch(name) or name in ("eval", "find"):
                problems.append("find %s runs %r, which the guard cannot verify" % (token, name or "nothing"))
            else:
                outside += action
                problems += script_problems(action, ctx, relative_ok, depth)
            k = end
        else:
            outside.append(token)
        k += 1
    problems += unverified(outside)
    if deleting:
        bad = [r for r in roots if not operand_ok(r, ctx, safe, relative_ok)]
        if not roots or bad:
            problems.append("find deleting outside the allowed locations: %s"
                            % (", ".join(repr(b) for b in bad[:3]) or "current directory"))
    return problems


def interpreter_script(name: str, args: list[str]) -> str | None:
    """The script an interpreter runs, or None when it runs none; ValueError when options cannot be parsed."""
    python = bool(PYTHON.fullmatch(name))
    k = 0
    while k < len(args):
        token = args[k]
        if token == "--":
            return args[k + 1] if k + 1 < len(args) else None
        if python and token == "-":
            return None
        if token in (PYTHON_VALUE_OPTIONS if python else SHELL_VALUE_OPTIONS):
            k += 2
            continue
        if re.fullmatch(r"[-+][A-Za-z]+", token):
            letters = token[1:]
            if python and ("m" in letters or "c" in letters):
                return None
            if python and letters[-1] in "WX":
                k += 2
                continue
            if not python and ("o" in letters or "O" in letters):
                k += 2
                continue
            k += 1
            continue
        if token.startswith("--"):
            if python and ("=" in token or token in ("--help", "--version")):
                k += 1
                continue
            if not python and token in SHELL_LONG_FLAGS:
                k += 1
                continue
            raise ValueError(token)
        return token
    return None


def script_problems(tokens: list[str], ctx: Context, relative_ok: bool, depth: int) -> list[str]:
    name = base(tokens[0])
    if INTERPRETER.fullmatch(name):
        stdin_source = None
        for k, token in enumerate(tokens[1:], 1):
            redirect = re.fullmatch(r"(?:\d+)?<(?!<)(.*)", token)
            if redirect:
                stdin_source = redirect.group(1) or (tokens[k + 1] if k + 1 < len(tokens) else "")
        args = drop_redirections(tokens[1:])
        if stdin_source:
            script = stdin_source
        elif name in ("source", "."):
            script = args[0] if args else None
        else:
            try:
                script = interpreter_script(name, args)
            except ValueError as exc:
                return ["%s options the guard cannot parse (%r)" % (name, str(exc))]
        python, by_path = bool(PYTHON.fullmatch(name)), False
    elif "/" in tokens[0]:
        script, python, by_path = tokens[0], False, True
    else:
        return []
    if not script:
        return []
    path = os.path.expanduser(os.path.expandvars(script))
    if "$" in path or "`" in path:
        return ["a script path the guard cannot resolve (%r)" % script]
    if not path.startswith("/"):
        if not relative_ok:
            return ["a relative script path after a directory change (%r)" % script]
        path = os.path.join(ctx.cwd, path)
    if os.path.realpath(path) == SELF:
        return []
    if not os.path.isfile(path):
        return ["a script that does not exist yet (%r)" % script]
    with open(path, "rb") as handle:
        raw = handle.read(SCRIPT_READ_LIMIT + 1)
    if by_path and b"\0" in raw[:4096]:
        return []
    if len(raw) > SCRIPT_READ_LIMIT:
        return ["a script too large to inspect (%r)" % script]
    text = raw.decode("utf-8", errors="ignore")
    found = remote_problems(text)
    trusted = under(os.path.normpath(os.path.abspath(path)), ctx.trusted_roots) or \
        under(os.path.realpath(path), ctx.trusted_roots)
    if not trusted:
        first_line = text.split("\n", 1)[0]
        if python or (by_path and first_line.startswith("#!") and "python" in first_line):
            found += python_problems(text, ctx)
        else:
            found += local_problems(text, ctx, depth + 1)
    return ["%s (in %s)" % (p, os.path.basename(path)) for p in found]


def segment_problems(raw: list[str], ctx: Context, safe: set[str], relative_ok: bool, depth: int) -> list[str]:
    tokens = strip_prefix(raw)
    problems = tamper_problems(raw) + unverified(raw[:len(raw) - len(tokens)])
    if not tokens:
        return problems
    name, args = base(tokens[0]), tokens[1:]
    if name in DELETE_COMMANDS:
        bad = [op for op in operands_of(args) if not operand_ok(op, ctx, safe, relative_ok)]
        if bad:
            problems.append("%s outside the allowed locations: %s" % (name, ", ".join(repr(b) for b in bad[:3])))
        return problems
    if name == "find":
        return problems + find_problems(args, ctx, safe, relative_ok, depth)
    if name == "eval":
        return problems + local_problems(" ".join(args), ctx, depth + 1)
    if name in SHELLS or PYTHON.fullmatch(name):
        flag = next((k for k, a in enumerate(args) if C_FLAG.fullmatch(a)), None)
        if flag is not None:
            code = args[flag + 1] if flag + 1 < len(args) else ""
            rest = unverified(args[:flag] + args[flag + 2:])
            if PYTHON.fullmatch(name):
                return problems + rest + python_problems(code, ctx)
            return problems + rest + local_problems(code, ctx, depth + 1)
    if name in WRAPPERS:
        runs = [a for a in args if base(a) in SHELLS or PYTHON.fullmatch(base(a)) or base(a) in ("eval", "find")]
        if runs:
            problems.append("%s runs %r, which the guard cannot verify" % (name, runs[0]))
        return problems + unverified(args)
    return problems + unverified(tokens) + script_problems(tokens, ctx, relative_ok, depth)


def local_problems(text: str, ctx: Context, depth: int = 0) -> list[str]:
    if depth > MAX_DEPTH:
        return ["commands nested too deeply to verify"]
    shell_text, shell_bodies, python_bodies = split_heredocs(text)
    problems = [p for body in shell_bodies for p in local_problems(body, ctx, depth + 1)]
    problems += [p for body in python_bodies for p in python_problems(body, ctx)]
    segs = split_shell(shell_text)
    raws = [words(seg) for seg, _ in segs]
    relative_ok = not any((lambda t: t and base(t[0]) in DIRECTORY_CHANGES)(strip_prefix(r)) for r in raws)
    safe_from = mktemp_assignments(segs, shell_text, ctx)
    for j, ((seg, _), raw) in enumerate(zip(segs, raws)):
        if MKTEMP_SEGMENT.fullmatch(seg):
            continue
        safe = {name for name, i in safe_from.items() if i < j}
        problems += segment_problems(raw, ctx, safe, relative_ok, depth)
    return problems


def remote_problems(text: str) -> list[str]:
    unquoted = re.sub(r"\$(?:''|\"\")", "", text)
    variants = [text, unquoted, re.sub(r"[\"'\\]", "", unquoted)]
    if not any(REMOTE.search(v) for v in variants):
        return []
    problems = []
    for label, pattern in REMOTE_RULES:
        m = next((hit for hit in (pattern.search(v) for v in variants) if hit), None)
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


def evaluate(command: str, project: str, cwd: str) -> list[str]:
    ctx = Context(project, cwd)
    return local_problems(command, ctx) + remote_problems(command)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        tool = payload.get("tool_name")
        if tool is not None and tool != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            raise ValueError("tool_input.command is missing")
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
        project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
        problems = evaluate(command, project, cwd)
    except Exception as exc:  # fail closed
        problems = ["the deletion guard could not evaluate this command (%s)" % type(exc).__name__]
    if not problems:
        return 0
    sys.stderr.write(
        "Blocked by the project deletion rule (CLAUDE.md, 'Deletion rule'): " + "; ".join(problems[:5])
        + ". On the Unraid server, delete only through the owning app's API (Sonarr/Radarr, qBittorrent, Plex). "
          "Locally, run rm/find -delete as a plain command (no wrapper, substitution or quoting around it) on "
          "literal paths inside /tmp, /private/tmp, /var/folders, $TMPDIR or this project's .claude/state/, or "
          "on a variable assigned from mktemp into a temp folder earlier in the same command. Keep local temp "
          "cleanup in a separate command from anything that uses ssh.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
