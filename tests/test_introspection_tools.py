"""get_current_time + get_current_session_info housekeeping tools."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from ark import db, runtime, tools
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.tools import ToolContext
from ark.types import TurnMetrics, UserText


def make_ctx(conn, tmp_path, *, session_id, model="claude-sonnet-4-6", max_ctx=None):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent = AgentConfig(
        name="scribe",
        provider="a",
        model=model,
        workspace=cwd,
        max_context_tokens=max_ctx,
    )
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"scribe": agent},
    )
    return ToolContext(
        conn=conn,
        config=cfg,
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        loaded_skills=set(),
    )


# ---------------------------------------------------------------------------
# get_current_time
# ---------------------------------------------------------------------------


def test_get_current_time_returns_expected_fields(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, tmp_path, session_id=sid)
    out, err = asyncio.run(tools.execute("get_current_time", {}, ctx=ctx))
    assert err is False
    payload = json.loads(out)
    assert {"iso", "unix_ms", "weekday", "tz"} <= payload.keys()
    assert payload["tz"] == "UTC"
    # iso parses
    from datetime import datetime

    assert datetime.fromisoformat(payload["iso"]).tzinfo is not None
    # unix_ms reasonable
    assert payload["unix_ms"] > 0


# ---------------------------------------------------------------------------
# get_current_session_info
# ---------------------------------------------------------------------------


def test_session_info_basic_fields(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, tmp_path, session_id=sid)
    out, err = asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))
    assert err is False
    info = json.loads(out)
    assert info["session_id"] == sid
    assert info["agent_name"] == "scribe"
    assert info["model"] == "claude-sonnet-4-6"
    assert info["kind"] == "conversational"
    assert isinstance(info["created_at"], int)
    assert info["message_count"] == 0
    assert info["last_input_tokens"] is None
    assert info["last_output_tokens"] is None
    assert info["context_window"] == 200_000  # from the model table


def test_session_info_reflects_message_count(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "heartbeat")
    runtime.append_message(conn, sid, UserText(text="one"))
    runtime.append_message(conn, sid, UserText(text="two"))
    runtime.append_message(conn, sid, UserText(text="three"))
    ctx = make_ctx(conn, tmp_path, session_id=sid)
    info = json.loads(asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))[0])
    assert info["message_count"] == 3
    assert info["kind"] == "heartbeat"


def test_session_info_uses_latest_turn_metrics(ark_home, tmp_path):
    """`last_input_tokens` reflects the most recent TurnMetrics row, which
    is the agent's best proxy for current context fill."""
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    runtime.append_message(conn, sid, TurnMetrics(input_tokens=1000, output_tokens=50))
    runtime.append_message(conn, sid, TurnMetrics(input_tokens=5400, output_tokens=128))
    ctx = make_ctx(conn, tmp_path, session_id=sid)
    info = json.loads(asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))[0])
    assert info["last_input_tokens"] == 5400
    assert info["last_output_tokens"] == 128


def test_session_info_context_window_honors_override(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, tmp_path, session_id=sid, max_ctx=1_000_000)
    info = json.loads(asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))[0])
    assert info["context_window"] == 1_000_000


def test_session_info_unknown_model_returns_null_window(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, tmp_path, session_id=sid, model="some-unknown-model")
    info = json.loads(asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))[0])
    assert info["context_window"] is None
