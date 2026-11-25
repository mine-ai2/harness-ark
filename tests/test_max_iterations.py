"""Configurable tool-loop ceiling (mine-capstone#1423).

`agents.<name>.max_iterations` sets how many provider calls one user turn may
make (default 16); session-create `metadata.max_iterations` may RAISE it, up
to `agents.<name>.max_iterations_ceiling` (default 64); the terminal `done`
frame carries `iterations` and `max_iterations`; `/health.features` advertises
`max_iterations_override`.
"""

import asyncio
import json
import os
import re
import string
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ark import config, db, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.server import create_app
from ark.types import AssistantTurnEnd, RunEnd, TextDelta, ToolCallEvent

REPO = Path(__file__).resolve().parent.parent


def make_config(workspace, **agent_extra):
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={
            "scribe": AgentConfig(
                name="scribe", provider="a", model="m", workspace=workspace,
                **agent_extra,
            )
        },
    )


@pytest.fixture(autouse=True)
def _clean_registries():
    runtime._active_turns.clear()
    runtime._turn_inboxes.clear()
    runtime._turn_steps.clear()
    yield
    runtime._active_turns.clear()
    runtime._turn_inboxes.clear()
    runtime._turn_steps.clear()


class _AlwaysTool:
    """Every provider call asks for another tool call — the loop only ends at
    its ceiling. Counts calls so tests can assert how far it ran."""

    def __init__(self):
        self.calls = 0

    async def stream_turn(self, **kwargs):
        self.calls += 1
        yield ToolCallEvent(id=f"t{self.calls}", name="nope", input={})
        yield AssistantTurnEnd(text="", stop_reason="tool_use")


class _ToolsThenText:
    """`tool_rounds` tool-call rounds, then a plain-text answer."""

    def __init__(self, tool_rounds):
        self.tool_rounds = tool_rounds
        self.calls = 0

    async def stream_turn(self, **kwargs):
        self.calls += 1
        if self.calls <= self.tool_rounds:
            yield ToolCallEvent(id=f"t{self.calls}", name="nope", input={})
            yield AssistantTurnEnd(text="", stop_reason="tool_use")
        else:
            yield TextDelta(text="ok")
            yield AssistantTurnEnd(text="ok", stop_reason="end_turn")


async def _run(cfg, conn, sid, provider, **kwargs):
    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="go",
        provider_factory=lambda *_a, **_k: provider,
        **kwargs,
    ):
        events.append(evt)
    return events


def _setup(tmp_path, metadata=None, **agent_extra):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = make_config(ws, **agent_extra)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational", metadata=metadata)
    return cfg, conn, sid


# --- config -----------------------------------------------------------------


def _write(home, agent_extra):
    (home / "config.json").write_text(json.dumps({
        "server": {"auth_secret": "shh"},
        "providers": {"anthropic": {"provider_type": "anthropic", "api_key": "k"}},
        "agents": {
            "scribe": {"provider": "anthropic", "model": "m", **agent_extra}
        },
    }))


def test_config_defaults(ark_home):
    _write(ark_home, {})
    agent = config.load().agents["scribe"]
    assert (agent.max_iterations, agent.max_iterations_ceiling) == (16, 64)


def test_config_parses_both_knobs(ark_home):
    _write(ark_home, {"max_iterations": 32, "max_iterations_ceiling": 48})
    agent = config.load().agents["scribe"]
    assert (agent.max_iterations, agent.max_iterations_ceiling) == (32, 48)


def test_config_ceiling_default_never_sits_below_the_baseline(ark_home):
    _write(ark_home, {"max_iterations": 100})
    agent = config.load().agents["scribe"]
    assert (agent.max_iterations, agent.max_iterations_ceiling) == (100, 100)


@pytest.mark.parametrize("key", ["max_iterations", "max_iterations_ceiling"])
@pytest.mark.parametrize("bad", [0, -1, "32", 3.5, True])
def test_config_rejects_non_positive_ints(ark_home, key, bad):
    _write(ark_home, {key: bad})
    with pytest.raises(config.ConfigError, match=key):
        config.load()


def test_config_rejects_a_ceiling_below_the_baseline(ark_home):
    _write(ark_home, {"max_iterations": 32, "max_iterations_ceiling": 16})
    with pytest.raises(config.ConfigError, match="max_iterations_ceiling"):
        config.load()


def test_talos_template_sets_32_under_a_64_ceiling(ark_home):
    """The deploy template is what harness-dev/prod actually run."""
    tmpl = (REPO / "deploy" / "config" / "config.json.tmpl").read_text()
    names = set(re.findall(r"\$\{(\w+)\}", tmpl))
    rendered = string.Template(tmpl).substitute(
        {n: ("7777" if n == "ARK_PORT" else "x") for n in names}
    )
    (ark_home / "config.json").write_text(rendered)
    talos = config.load().agents["talos"]
    assert (talos.max_iterations, talos.max_iterations_ceiling) == (32, 64)


# --- the loop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_agent_still_stops_at_16(ark_home, tmp_path):
    cfg, conn, sid = _setup(tmp_path)
    provider = _AlwaysTool()
    events = await _run(cfg, conn, sid, provider)
    assert provider.calls == 16
    assert events[-1] == RunEnd(
        stop_reason="max_iterations", iterations=16, max_iterations=16
    )


@pytest.mark.asyncio
async def test_configured_32_runs_32(ark_home, tmp_path):
    cfg, conn, sid = _setup(tmp_path, max_iterations=32)
    provider = _AlwaysTool()
    events = await _run(cfg, conn, sid, provider)
    assert provider.calls == 32
    assert events[-1] == RunEnd(
        stop_reason="max_iterations", iterations=32, max_iterations=32
    )


@pytest.mark.asyncio
async def test_metadata_raises_up_to_the_ceiling(ark_home, tmp_path):
    cfg, conn, sid = _setup(
        tmp_path, metadata={"max_iterations": 48},
        max_iterations=32, max_iterations_ceiling=64,
    )
    provider = _AlwaysTool()
    events = await _run(cfg, conn, sid, provider)
    assert provider.calls == 48
    assert events[-1].max_iterations == 48


@pytest.mark.asyncio
async def test_metadata_above_the_ceiling_is_clamped(ark_home, tmp_path):
    cfg, conn, sid = _setup(
        tmp_path, metadata={"max_iterations": 100},
        max_iterations=32, max_iterations_ceiling=64,
    )
    provider = _AlwaysTool()
    events = await _run(cfg, conn, sid, provider)
    assert provider.calls == 64
    assert events[-1] == RunEnd(
        stop_reason="max_iterations", iterations=64, max_iterations=64
    )


@pytest.mark.asyncio
async def test_metadata_never_lowers_the_configured_value(ark_home, tmp_path):
    cfg, conn, sid = _setup(
        tmp_path, metadata={"max_iterations": 4}, max_iterations=32
    )
    provider = _AlwaysTool()
    await _run(cfg, conn, sid, provider)
    assert provider.calls == 32


@pytest.mark.parametrize("bad", ["48", 48.0, True, 0, -5, None, [48], {"n": 48}])
def test_invalid_metadata_values_are_ignored(ark_home, tmp_path, bad):
    cfg, conn, sid = _setup(tmp_path, metadata={"max_iterations": bad})
    _model, preset = runtime._session_overrides(conn, sid, cfg.agents["scribe"])
    assert "max_iterations" not in preset
    assert runtime._resolve_max_iterations(cfg.agents["scribe"], None, preset) == 16


def test_effort_high_and_metadata_take_the_larger_under_the_ceiling(ark_home, tmp_path):
    agent = make_config(tmp_path).agents["scribe"]  # 16 / ceiling 64
    resolve = runtime._resolve_max_iterations
    assert resolve(agent, None, {}) == 16
    assert resolve(agent, None, dict(runtime.EFFORT_PRESETS["high"])) == 32
    cfg, conn, sid = _setup(tmp_path, metadata={"effort": "high", "max_iterations": 20})
    _m, preset = runtime._session_overrides(conn, sid, cfg.agents["scribe"])
    assert resolve(agent, None, preset) == 32  # effort's 32 beats metadata's 20
    cfg, conn, sid = _setup(tmp_path, metadata={"effort": "high", "max_iterations": 40})
    _m, preset = runtime._session_overrides(conn, sid, cfg.agents["scribe"])
    assert resolve(agent, None, preset) == 40
    # The shared preset table is never mutated by a session's value.
    assert runtime.EFFORT_PRESETS["high"]["max_iterations"] == 32
    # A tight ceiling bounds the effort preset too.
    tight = make_config(tmp_path, max_iterations=16, max_iterations_ceiling=24)
    assert resolve(tight.agents["scribe"], None, dict(runtime.EFFORT_PRESETS["high"])) == 24
    # An explicit caller value is a base the ceiling never lowers.
    assert resolve(agent, 100, {}) == 100


# --- `done` on the wire -----------------------------------------------------


@pytest.mark.asyncio
async def test_normal_end_reports_provider_calls_made(ark_home, tmp_path):
    cfg, conn, sid = _setup(tmp_path, max_iterations=32)
    events = await _run(cfg, conn, sid, _ToolsThenText(tool_rounds=2))
    assert events[-1] == RunEnd(stop_reason="end_turn", iterations=3, max_iterations=32)
    assert runtime.event_to_wire(events[-1]) == {
        "type": "done", "stop_reason": "end_turn", "iterations": 3, "max_iterations": 32,
    }


@pytest.mark.asyncio
async def test_error_end_reports_the_failed_call(ark_home, tmp_path):
    class _Boom:
        async def stream_turn(self, **kwargs):
            raise RuntimeError("bad request: nope")
            yield  # pragma: no cover

    cfg, conn, sid = _setup(tmp_path)
    events = await _run(cfg, conn, sid, _Boom())
    end = events[-1]
    assert isinstance(end, RunEnd) and end.stop_reason.startswith("error:")
    assert (end.iterations, end.max_iterations) == (1, 16)


def test_wire_omits_the_keys_when_unknown():
    assert runtime.event_to_wire(RunEnd(stop_reason="end_turn")) == {
        "type": "done", "stop_reason": "end_turn",
    }


def _recv_until(ws, event_type, sid, tries=200):
    for _ in range(tries):
        frame = ws.receive_json()
        if frame.get("type") == event_type and frame.get("session_id") == sid:
            return frame
    raise AssertionError(f"never saw {event_type!r} for {sid!r}")


def test_ws_done_carries_the_numbers_from_session_metadata(ark_home, tmp_path, monkeypatch):
    """End to end: REST create with metadata → WS turn → `done` frame."""
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    provider = _AlwaysTool()
    monkeypatch.setattr(runtime, "make_provider", lambda *_a, **_k: provider)
    client = TestClient(create_app(make_config(ws_dir, max_iterations=3, max_iterations_ceiling=5)))
    conn = db.init_db()
    sid = runtime.create_session(
        conn, "scribe", "conversational", metadata={"max_iterations": 9}
    )
    with client.websocket_connect("/events?token=x") as ws:
        ws.send_json({"type": "user_message", "session_id": sid, "text": "go"})
        done = _recv_until(ws, "done", sid)
    assert done["stop_reason"] == "max_iterations"
    assert (done["iterations"], done["max_iterations"]) == (5, 5)
    assert provider.calls == 5
    assert runtime._turn_steps == {}  # registry cleared with the turn


def test_stopped_done_carries_the_numbers(ark_home, tmp_path, monkeypatch):
    class _Hang:
        async def stream_turn(self, **kwargs):
            yield TextDelta(text="partial…")
            await asyncio.Event().wait()

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    monkeypatch.setattr(runtime, "make_provider", lambda *_a, **_k: _Hang())
    client = TestClient(create_app(make_config(ws_dir, max_iterations=32)))
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    with client.websocket_connect("/events?token=x") as ws:
        ws.send_json({"type": "user_message", "session_id": sid, "text": "go"})
        _recv_until(ws, "assistant_delta", sid)
        ws.send_json({"type": "stop", "session_id": sid})
        done = _recv_until(ws, "done", sid)
    assert done["stopped"] is True and done["stop_reason"] == "stopped"
    assert (done["iterations"], done["max_iterations"]) == (1, 32)
    assert runtime._turn_steps == {}


# --- /health ----------------------------------------------------------------


def test_health_advertises_max_iterations_override(ark_home, tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    client = TestClient(create_app(make_config(ws_dir)))
    assert client.get("/health").json() == {
        "ok": True,
        "features": ["mid_turn_input", "max_iterations_override"],
    }


def test_smoke_script_accepts_the_health_body():
    """deploy-dev runs deploy/scripts/smoke.sh after every merge; its /health
    assertion must tolerate additive features (harness-ark PR #25 lesson).
    Runs the exact python the script embeds against the new body."""
    script = (REPO / "deploy" / "scripts" / "smoke.sh").read_text()
    m = re.search(r"python3 - \"\$body\" <<'PY'.*?\n(.*?)\nPY\n", script, re.S)
    assert m, "smoke.sh /health check moved — update this test"
    body = json.dumps({"ok": True, "features": ["mid_turn_input", "max_iterations_override"]})
    import subprocess, sys

    proc = subprocess.run(
        [sys.executable, "-", body], input=m.group(1), text=True,
        capture_output=True, env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr


def test_cli_names_the_limit_on_a_capped_turn(capsys):
    from ark import cli

    ui = cli._Ui()
    ui.is_tty = False
    cli._handle_event(
        ui, {"type": "done", "stop_reason": "max_iterations", "iterations": 32, "max_iterations": 32}
    )
    cli._handle_event(ui, {"type": "done", "stop_reason": "end_turn", "iterations": 2, "max_iterations": 32})
    err = capsys.readouterr().err
    assert err.count("tool-step limit") == 1
    assert "stopped at the tool-step limit (32)" in err
