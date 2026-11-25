"""Detach guard for `run_command` (mine-capstone#1418).

`run_command` runs a command in the foreground, in its own process group, so
that `stop` and the timeout can kill the whole tree. A command that detaches
(`setsid`, `nohup … &`, `disown`, a terminal multiplexer, a scheduler, or a
plain background `&`) escapes that: it outlives the turn and ignores Stop.
`detach_reason()` names the offending construct so the tool can refuse to
spawn and teach the rule instead.

This is a shell-aware scan, not a regex: quotes, escapes, comments, here-docs,
redirections (`2>&1`, `>&2`, `&>`), `&&`, `|&` and arithmetic are understood,
so `echo "at noon"`, `grep at file`, `--format=at` and `/usr/bin/at-spi` do
not trip it. It is a guard against the model's habits, not a sandbox: a script
written to disk and then executed is not inspected (the cgroup kill in
mine-capstone#1434 is the hard stop).

Rules:

* Detach commands — `setsid nohup disown screen tmux systemd-run crontab at` —
  are rejected in command position (first word of a simple command, after
  `VAR=x` assignments), also by path (`/usr/bin/setsid`), and behind a wrapper
  (`sudo`, `env`, `timeout 10`, `xargs`, `nice`, …). Behind a wrapper every
  later word is checked for the distinctive names; `at` (an English word) only
  counts as the wrapper's effective command.
* A background `&` is rejected unless the job is reaped in the foreground: all
  `&` at the top level (not inside a `( … )` subshell) and a `wait` command
  after the last one (`a & b & wait`).
* `sh -c '…'`, `bash -lc "…"`, `eval …` and a here-doc fed to a shell are
  scanned recursively. Anything else inside quotes is data and is allowed.
* A command the scanner cannot parse (unterminated quote) is let through —
  the shell will reject it too.
"""

from __future__ import annotations

import re

DETACH_COMMANDS = frozenset(
    {"setsid", "nohup", "disown", "screen", "tmux", "systemd-run", "crontab", "at"}
)
# English words: only ever judged in command position.
_AMBIGUOUS = frozenset({"at"})
_WRAPPERS = frozenset(
    {
        "sudo", "doas", "env", "exec", "command", "builtin", "nice", "ionice",
        "time", "timeout", "stdbuf", "xargs", "chrt", "taskset", "unbuffer",
        "watch", "flock", "nsenter",
    }
)
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
# Reserved words after which the next word is still in command position.
_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "}", "time"}
)
_CONTROL_OPS = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "\n", "`"})
_REDIRECT_OPS = frozenset({">", ">>", "<", ">&", "<&", "&>", "&>>", "<<<", ">|", "<>"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_NUMBERISH_RE = re.compile(r"^[0-9.]+[smhd]?$")
_MAX_DEPTH = 4


class _Unparseable(Exception):
    pass


def _tokenize(command: str) -> list[tuple[str, str]]:
    """Return `(kind, value)` tokens: `word` (quotes removed), `op`, and
    `heredoc` (the body, emitted where its `<<` operator stood)."""

    tokens: list[tuple[str, str]] = []
    pending: list[tuple[int, str, bool]] = []  # (token index, delimiter, strip tabs)
    buf: list[str] = []
    in_word = False
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal in_word
        if in_word:
            tokens.append(("word", "".join(buf)))
            buf.clear()
            in_word = False

    def op(value: str) -> None:
        flush()
        tokens.append(("op", value))

    def read_heredocs(pos: int) -> int:
        """`pos` is just past a newline; consume the pending here-doc bodies."""
        for index, delim, strip_tabs in pending:
            lines: list[str] = []
            while pos <= n:
                end = command.find("\n", pos)
                line = command[pos:] if end == -1 else command[pos:end]
                pos = n + 1 if end == -1 else end + 1
                probe = line.lstrip("\t") if strip_tabs else line
                if probe == delim:
                    break
                lines.append(line)
            tokens[index] = ("heredoc", "\n".join(lines))
        pending.clear()
        return min(pos, n)

    while i < n:
        c = command[i]
        if c == "\\":
            if i + 1 < n and command[i + 1] == "\n":  # line continuation
                i += 2
                continue
            if i + 1 < n:
                buf.append(command[i + 1])
            in_word = True
            i += 2
            continue
        if c == "'":
            ansi = bool(buf) and buf[-1] == "$" and in_word
            if ansi:
                buf.pop()
            j = i + 1
            while True:
                if j >= n:
                    raise _Unparseable
                if ansi and command[j] == "\\" and j + 1 < n:
                    buf.append(command[j + 1])
                    j += 2
                    continue
                if command[j] == "'":
                    break
                buf.append(command[j])
                j += 1
            in_word = True
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while True:
                if j >= n:
                    raise _Unparseable
                if command[j] == "\\" and j + 1 < n:
                    if command[j + 1] in '"\\$`\n':
                        if command[j + 1] != "\n":
                            buf.append(command[j + 1])
                    else:
                        buf.append(command[j : j + 2])
                    j += 2
                    continue
                if command[j] == '"':
                    break
                buf.append(command[j])
                j += 1
            in_word = True
            i = j + 1
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c == "\n":
            op("\n")
            i += 1
            if pending:
                i = read_heredocs(i)
            continue
        if c == "#" and not in_word:
            end = command.find("\n", i)
            i = n if end == -1 else end
            continue
        if c == "(" and command.startswith("((", i):
            # Arithmetic — `$(( a & b ))` / `(( i & 1 ))`: `&` is bitwise.
            end = command.find("))", i + 2)
            if end == -1:
                raise _Unparseable
            buf.append("0")
            in_word = True
            i = end + 2
            continue
        if c == "&":
            if command.startswith("&&", i):
                op("&&")
                i += 2
            elif command.startswith("&>>", i):
                op("&>>")
                i += 3
            elif command.startswith("&>", i):
                op("&>")
                i += 2
            else:
                op("&")
                i += 1
            continue
        if c == "|":
            two = command[i : i + 2]
            if two in ("||", "|&"):
                op(two)
                i += 2
            else:
                op("|")
                i += 1
            continue
        if c == ";":
            if command.startswith(";;", i):
                op(";;")
                i += 2
            else:
                op(";")
                i += 1
            continue
        if c in "()`":
            op(c)
            i += 1
            continue
        if c in "<>":
            # A bare number glued to a redirect is its fd (`2>&1`), not a word.
            if in_word and "".join(buf).isdigit():
                buf.clear()
                in_word = False
            if command.startswith("<<<", i):
                op("<<<")
                i += 3
            elif command.startswith("<<", i):
                flush()
                i += 2
                strip_tabs = i < n and command[i] == "-"
                if strip_tabs:
                    i += 1
                while i < n and command[i] in " \t":
                    i += 1
                j = i
                while j < n and command[j] not in " \t\n;&|()<>":
                    j += 1
                delim = command[i:j].replace("'", "").replace('"', "").replace("\\", "")
                if not delim:
                    raise _Unparseable
                pending.append((len(tokens), delim, strip_tabs))
                tokens.append(("heredoc", ""))
                i = j
            else:
                for candidate in (">>", ">&", ">|", "<&", "<>"):
                    if command.startswith(candidate, i):
                        op(candidate)
                        i += 2
                        break
                else:
                    op(c)
                    i += 1
            continue
        buf.append(c)
        in_word = True
        i += 1
    flush()
    return tokens


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _strip_prefix(words: list[str]) -> list[str]:
    """Drop leading `VAR=x` assignments and reserved words: what is left
    starts with the command word."""
    k = 0
    while k < len(words) and (_ASSIGNMENT_RE.match(words[k]) or words[k] in _KEYWORDS):
        k += 1
    return words[k:]


def _check_simple(words: list[str], heredocs: list[str], depth: int) -> str | None:
    words = _strip_prefix(words)
    if not words:
        return None
    cmd = _base(words[0])
    if cmd in DETACH_COMMANDS:
        return f"`{cmd}`"
    rest = words[1:]
    if cmd in _SHELLS:
        k = 0
        while k < len(rest):
            word = rest[k]
            if word in ("-o", "+o", "-O", "+O"):
                k += 2
                continue
            if not word.startswith("-") or word.startswith("--"):
                if not word.startswith("--"):
                    break  # a script file: not inspected
                k += 1
                continue
            if "c" in word:
                if k + 1 < len(rest):
                    return _scan(rest[k + 1], depth + 1)
                break
            k += 1
        for body in heredocs:
            reason = _scan(body, depth + 1)
            if reason:
                return reason
        return None
    if cmd == "eval":
        return _scan(" ".join(rest), depth + 1)
    if cmd in _WRAPPERS:
        if cmd == "command" and any(w in ("-v", "-V") for w in rest):
            return None  # a lookup, nothing runs
        for word in rest:
            name = _base(word)
            if not word.startswith("-") and name in DETACH_COMMANDS - _AMBIGUOUS:
                return f"`{name}`"
        for k, word in enumerate(rest):
            if word.startswith("-") or _ASSIGNMENT_RE.match(word) or _NUMBERISH_RE.match(word):
                continue
            return _check_simple(rest[k:], heredocs, depth)
    return None


def _scan(command: str, depth: int = 0) -> str | None:
    if depth > _MAX_DEPTH:
        return None
    tokens = _tokenize(command)

    words: list[str] = []
    heredocs: list[str] = []
    paren = 0
    background: list[int] = []  # paren depth of each background `&`
    wait_after_last_bg = False
    skip_next_word = False

    def end_simple() -> str | None:
        nonlocal words, heredocs, wait_after_last_bg
        probe = _strip_prefix(words)
        if probe and probe[0] == "wait" and paren == 0:
            wait_after_last_bg = True
        reason = _check_simple(words, heredocs, depth)
        words, heredocs = [], []
        return reason

    for kind, value in tokens:
        if kind == "word":
            if skip_next_word:
                skip_next_word = False
                continue
            words.append(value)
            continue
        if kind == "heredoc":
            heredocs.append(value)
            continue
        if value in _REDIRECT_OPS:
            skip_next_word = True
            continue
        skip_next_word = False
        if value in _CONTROL_OPS:
            reason = end_simple()
            if reason:
                return reason
            if value == "(":
                paren += 1
            elif value == ")":
                paren = max(0, paren - 1)
            elif value == "&":
                background.append(paren)
                wait_after_last_bg = False
    reason = end_simple()
    if reason:
        return reason
    if background and not (all(d == 0 for d in background) and wait_after_last_bg):
        return "a background `&`"
    return None


def detach_reason(command: str) -> str | None:
    """Name the construct that would detach `command` from its foreground
    process group, or None when the command stays in the foreground."""

    try:
        return _scan(command)
    except _Unparseable:
        return None
