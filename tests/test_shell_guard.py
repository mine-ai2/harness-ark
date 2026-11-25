"""run_command detach guard (mine-capstone#1418): the allow / reject table."""

import pytest

from ark.shell_guard import detach_reason

ALLOWED = [
    "echo a && echo b",
    "cmd 2>&1",
    "ls |& tee out.txt",
    "echo hi >&2",
    "make &> build.log",
    "make &>> build.log",
    "exec 3>&1",
    'echo "a & b"',
    "echo 'run setsid &'",
    "echo a \\& b",
    'echo "at noon"',
    "cat file | grep at",
    "sort --format=at x",
    "/usr/bin/at-spi-bus-launcher --help",
    "FOO=1 echo at",
    "sudo grep at file",
    "for at in 1 2; do echo $at; done",
    'curl "https://x.test/?a=1&b=2"',
    "python3 - <<'PY'\nx = 3 & 5\nprint('at', x)\nPY",
    "cat <<EOF\nrun in tmux & detach\nEOF",
    "cat <<-EOF\n\tnohup &\n\tEOF\necho ok",
    "echo $((6 & 3))",
    "a & b & wait",
    "sleep 1 & pid=$!; echo hi; wait $pid",
    'git commit -m "nohup fix"',
    "ps aux | grep screen",
    "command -v tmux",
    "which setsid",
    "echo done # run later with nohup &",
    "if true; then echo x; fi",
    "x=$(date +%s); echo $x",
    "bash script.sh",
    'bash -c "echo a && echo b"',
    'echo "unterminated',  # unparseable: the shell rejects it itself
]

REJECTED = [
    ("setsid sleep 100 &", "setsid"),
    ("sleep 100 &", "background"),
    ("sleep 9 &\n", "background"),
    ("nohup python3 job.py > log 2>&1 &", "nohup"),
    ("sleep 5 & disown", "disown"),
    ("screen -dmS job ./run.sh", "screen"),
    ("tmux new -d ./run.sh", "tmux"),
    ("systemd-run --user sleep 100", "systemd-run"),
    ("crontab -l", "crontab"),
    ('echo "sleep 9" | at now', "`at`"),
    ("at now + 1 minute", "`at`"),
    ("/usr/bin/setsid sleep 9", "setsid"),
    ('"setsid" sleep 9', "setsid"),
    ("FOO=1 setsid sleep 9", "setsid"),
    ("true && setsid sleep 9", "setsid"),
    ("if true; then nohup x; fi", "nohup"),
    ("echo $(setsid sleep 9)", "setsid"),
    ("echo `nohup sleep 9`", "nohup"),
    ("timeout 10 setsid sleep 9", "setsid"),
    ("timeout -s KILL 10 setsid sleep 9", "setsid"),
    ("env X=1 nohup sleep 9", "nohup"),
    ("env at now", "`at`"),
    ("ls | xargs -n 1 nohup sh", "nohup"),
    ("(sleep 100 &)", "background"),
    ("(sleep 100 &); wait", "background"),
    ("a & b", "background"),
    ("wait; sleep 9 &", "background"),
    ("sleep 100 > /dev/null 2>&1 & echo started", "background"),
    ("curl https://x.test/?a=1&b=2", "background"),
    ("bash -c 'sleep 100 &'", "background"),
    ('bash -lc "setsid sleep 9"', "setsid"),
    ('bash -o pipefail -c "nohup x"', "nohup"),
    ('eval "sleep 100 &"', "background"),
    ("bash <<EOF\nsetsid sleep 9\nEOF", "setsid"),
]


@pytest.mark.parametrize("command", ALLOWED)
def test_foreground_commands_are_allowed(command):
    assert detach_reason(command) is None


@pytest.mark.parametrize("command,names", REJECTED)
def test_detaching_commands_are_named(command, names):
    reason = detach_reason(command)
    assert reason is not None, command
    assert names in reason
