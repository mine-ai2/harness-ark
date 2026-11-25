# Built-in tools — execution rules

The built-in tool list is in [design/design.md](../design/design.md) §"Built-in
tools"; tool configuration (search, fetch) is in [config.md](config.md). This
page documents the rules a built-in tool enforces on its own input.

## `run_command`

`run_command(command, timeout_seconds=60)` runs `command` through the shell
**in the foreground**, in its own process group, for at most 600 seconds, and
returns stdout, stderr and the exit code. `stop` and the timeout SIGTERM the
whole process group (SIGKILL after 5 s).

### No detaching

A command that leaves that process group — or outlives the shell that started
it — would survive the turn and ignore Stop. Such commands are **rejected
before anything is spawned**, with an error that names the construct and says
what to do instead (chunk the work into commands that each finish, run
parallel steps as `a & b & wait`, or ask for a bulk tool).

Rejected:

| Construct | Examples |
|---|---|
| A detach command in command position: `setsid`, `nohup`, `disown`, `screen`, `tmux`, `systemd-run`, `crontab`, `at` | `setsid sleep 100`, `/usr/bin/setsid x`, `FOO=1 nohup x`, `if ok; then nohup x; fi`, `echo job \| at now`, `echo $(setsid x)` |
| The same behind a wrapper (`sudo`, `env`, `exec`, `command`, `nice`, `timeout`, `xargs`, `stdbuf`, `watch`, `flock`, …) | `timeout 10 setsid x`, `env A=1 nohup x`, `ls \| xargs -n 1 nohup sh` |
| A background `&` that nothing reaps | `sleep 100 &`, `job > log 2>&1 & echo started`, `a & b`, `(sleep 100 &)`, `(sleep 100 &); wait`, an unquoted URL `curl https://h/?a=1&b=2` |
| Any of the above inside a nested shell | `bash -c 'sleep 100 &'`, `bash -lc "setsid x"`, `eval "x &"`, `bash <<EOF … setsid x … EOF` |

Allowed:

| Construct | Examples |
|---|---|
| `&&`, `\|\|`, `\|&` | `echo a && echo b`, `make \|& tee out.txt` |
| Redirections | `cmd 2>&1`, `echo hi >&2`, `make &> build.log`, `exec 3>&1` |
| `&` or a detach word inside quotes, escaped, or in a comment | `echo "a & b"`, `curl "https://h/?a=1&b=2"`, `git commit -m "nohup fix"`, `echo a \& b` |
| A detach word that is an argument, not the command | `grep at file`, `sort --format=at`, `ps aux \| grep screen`, `which setsid`, `command -v tmux`, `/usr/bin/at-spi-bus-launcher` |
| Arithmetic and here-doc bodies fed to a non-shell program | `echo $((6 & 3))`, `python3 - <<'PY' … x = a & b … PY` |
| Background jobs reaped in the foreground: every `&` at the top level and a `wait` after the last one | `a & b & wait`, `sleep 1 & pid=$!; …; wait $pid` |

Choices worth knowing:

- Behind a wrapper, **every** later word is checked for the distinctive names
  (so `timeout -s KILL 10 setsid x` is caught); the price is a false positive
  on e.g. `sudo apt-get install tmux`. `at` is an English word, so it only
  counts as the wrapper's effective command (`env at now`, not
  `sudo grep at file`).
- Double-quoted text is treated as data, including a `$(…)` inside it.
- A command the scanner cannot parse (an unterminated quote) is passed to the
  shell, which rejects it as well.
- This is a guard against habits, not a sandbox: a script written to disk and
  then executed is not inspected. Killing survivors by cgroup is tracked in
  mine-capstone#1434.

Implementation: `ark/shell_guard.py` (`detach_reason`), called at the top of
`_run_command` in `ark/tools.py`.
