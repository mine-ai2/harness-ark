"""Schedule meta-tool: set_heartbeat / add_cron / list_crons / remove_cron."""

import asyncio

import pytest

from ark import db, tools
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.tools import ToolContext


def make_ctx(conn, tmp_path, agent_name="scribe"):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent = AgentConfig(
        name=agent_name,
        provider="anthropic",
        model="m",
        workspace=cwd,
    )
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"anthropic": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={agent_name: agent},
    )
    return ToolContext(
        conn=conn, config=cfg, agent=agent, session_id="s", cwd=cwd, loaded_skills=set()
    )


def test_set_heartbeat(ark_home, tmp_path):
    conn = db.init_db()
    ctx = make_ctx(conn, tmp_path)
    output, err = asyncio.run(tools.execute("set_heartbeat", {"seconds": 60}, ctx=ctx))
    assert err is False
    row = conn.execute(
        "SELECT heartbeat_seconds FROM agent_state WHERE agent_name = 'scribe'"
    ).fetchone()
    assert row["heartbeat_seconds"] == 60

    # disable
    asyncio.run(tools.execute("set_heartbeat", {"seconds": None}, ctx=ctx))
    row = conn.execute(
        "SELECT heartbeat_seconds FROM agent_state WHERE agent_name = 'scribe'"
    ).fetchone()
    assert row["heartbeat_seconds"] is None


def test_add_and_list_and_remove_cron(ark_home, tmp_path):
    conn = db.init_db()
    ctx = make_ctx(conn, tmp_path)
    output, err = asyncio.run(
        tools.execute(
            "add_cron",
            {"id": "morning", "expr": "0 9 * * *", "prompt": "give the briefing"},
            ctx=ctx,
        )
    )
    assert err is False, output

    output, err = asyncio.run(tools.execute("list_crons", {}, ctx=ctx))
    assert err is False
    assert "morning" in output
    assert "0 9 * * *" in output

    output, err = asyncio.run(tools.execute("remove_cron", {"id": "morning"}, ctx=ctx))
    assert err is False
    output, err = asyncio.run(tools.execute("list_crons", {}, ctx=ctx))
    assert "(no crons)" in output


def test_add_cron_validates_expression(ark_home, tmp_path):
    conn = db.init_db()
    ctx = make_ctx(conn, tmp_path)
    output, err = asyncio.run(
        tools.execute(
            "add_cron",
            {"id": "bad", "expr": "this is not cron", "prompt": "x"},
            ctx=ctx,
        )
    )
    assert err is True
    assert "invalid" in output.lower()


def test_remove_unknown_cron(ark_home, tmp_path):
    conn = db.init_db()
    ctx = make_ctx(conn, tmp_path)
    output, err = asyncio.run(tools.execute("remove_cron", {"id": "ghost"}, ctx=ctx))
    assert err is True
    assert "no cron" in output
