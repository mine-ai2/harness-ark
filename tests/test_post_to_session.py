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


def test_history_renders_injected_messages_distinctly(ark_home, tmp_path):
    """Injected messages should show up in history as `InjectedMessage` —
    same shape as the live WS `injected_message` event — not as a plain
    `AssistantText` with an extra field. Otherwise web clients that special-
    case the live event don't recognize them on reload."""
    from fastapi.testclient import TestClient

    from ark import runtime
    from ark.server import create_app
    from ark.types import AssistantText

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = make_config()
    cfg.agents["scribe"].workspace = workspace
    app = create_app(cfg)
    client = TestClient(app)
    H = {"Authorization": "Bearer x"}

    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    runtime.append_message(
        app.state.conn,
        sid,
        AssistantText(text="normal reply"),  # no injected_from
    )
    runtime.append_message(
        app.state.conn,
        sid,
        AssistantText(text="cron tick!", injected_from="source-session-xyz"),
    )

    r = client.get(f"/agents/scribe/sessions/{sid}/history", headers=H)
    assert r.status_code == 200
    hist = r.json()
    assert len(hist) == 2
    assert hist[0]["kind"] == "AssistantText"
    assert hist[0]["data"]["text"] == "normal reply"
    # The injected one uses a distinct kind and the same field name (`from_session_id`)
    # the live WS event uses, so clients have a single render path.
    assert hist[1]["kind"] == "InjectedMessage"
    assert hist[1]["data"] == {"text": "cron tick!", "from_session_id": "source-session-xyz"}


def test_history_endpoint_serializes_tool_call_with_thought_signature(ark_home, tmp_path):
    """Regression: a ToolCall with a binary thought_signature used to make
    GET /history return 500 because FastAPI's default bytes encoder assumes
    UTF-8. We strip the field at the serialization boundary — clients don't
    need it."""
    from fastapi.testclient import TestClient

    from ark import db, runtime
    from ark.server import create_app
    from ark.types import ToolCall

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = make_config()
    cfg.agents["scribe"].workspace = workspace
    app = create_app(cfg)
    client = TestClient(app)
    H = {"Authorization": "Bearer x"}

    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    # Inject a ToolCall with a binary signature directly (simulates Gemini state).
    runtime.append_message(
        app.state.conn,
        sid,
        ToolCall(id="t1", name="x", input={}, thought_signature=b"\xab\xcd\xef"),
    )

    r = client.get(f"/agents/scribe/sessions/{sid}/history", headers=H)
    assert r.status_code == 200, r.text
    history = r.json()
    assert len(history) == 1
    assert history[0]["kind"] == "ToolCall"
    # thought_signature must NOT be in the wire response
    assert "thought_signature" not in history[0]["data"]
    # ... but id/name/input must be there
    assert history[0]["data"]["id"] == "t1"


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
