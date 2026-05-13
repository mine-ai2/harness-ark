"""Cross-session messaging via post_to_session + broker."""

import asyncio
import json

import pytest

from ark import broker, db, runtime, tools
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.tools import ToolContext
from ark.types import AssistantText


def make_config():
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={
            "anthropic": ProviderConfig(provider_type="anthropic", api_key="k")
        },
        tools={},
        agents={
            "scribe": AgentConfig(
                name="scribe",
                provider="anthropic",
                model="m",
                workspace=None,  # we won't use it
            )
        },
    )


def make_ctx(conn, agent, *, session_id, tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent.workspace = cwd  # set the workspace for tool cwd
    return ToolContext(
        conn=conn,
        config=make_config(),
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        loaded_skills=set(),
    )


@pytest.mark.asyncio
async def test_post_to_session_writes_assistant_message(ark_home, tmp_path):
    conn = db.init_db()
    cfg = make_config()
    agent = cfg.agents["scribe"]
    source = runtime.create_session(conn, "scribe", "heartbeat")
    target = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, agent, session_id=source, tmp_path=tmp_path)

    output, err = await tools.execute(
        "post_to_session", {"session_id": target, "body": "hello target"}, ctx=ctx
    )
    assert err is False, output

    history = runtime.load_history(conn, target)
    assert len(history) == 1
    assert isinstance(history[0], AssistantText)
    assert history[0].text == "hello target"
    assert history[0].injected_from == source


@pytest.mark.asyncio
async def test_post_to_session_rejects_unknown_session(ark_home, tmp_path):
    conn = db.init_db()
    cfg = make_config()
    agent = cfg.agents["scribe"]
    source = runtime.create_session(conn, "scribe", "heartbeat")
    ctx = make_ctx(conn, agent, session_id=source, tmp_path=tmp_path)
    output, err = await tools.execute(
        "post_to_session", {"session_id": "does-not-exist", "body": "x"}, ctx=ctx
    )
    assert err is True
    assert "not found" in output


@pytest.mark.asyncio
async def test_post_to_session_publishes_to_broker(ark_home, tmp_path):
    conn = db.init_db()
    cfg = make_config()
    agent = cfg.agents["scribe"]
    source = runtime.create_session(conn, "scribe", "heartbeat")
    target = runtime.create_session(conn, "scribe", "conversational")
    queue = broker.subscribe(target)
    try:
        ctx = make_ctx(conn, agent, session_id=source, tmp_path=tmp_path)
        await tools.execute(
            "post_to_session", {"session_id": target, "body": "ping"}, ctx=ctx
        )
        evt = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert evt["type"] == "injected_message"
        assert evt["from_session_id"] == source
        assert evt["text"] == "ping"
    finally:
        broker.unsubscribe(target, queue)
