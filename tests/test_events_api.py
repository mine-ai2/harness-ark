"""Unified per-client event API — GET /events catch-up + WS /events firehose."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ark import broker, db, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.server import create_app
from ark.types import (
    AssistantText,
    AssistantTurnEnd,
    SessionContext,
    UserText,
)


def make_config(workspace):
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={
            "scribe": AgentConfig(
                name="scribe", provider="a", model="m", workspace=workspace
            )
        },
    )


def _make_client(ark_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    app = create_app(make_config(ws))
    return TestClient(app), app


# ---------------------------------------------------------------------------
# GET /events — catch-up over persisted messages, cross-session
# ---------------------------------------------------------------------------


def test_get_events_returns_messages_across_sessions(ark_home, tmp_path):
    client, app = _make_client(ark_home, tmp_path)
    H = {"Authorization": "Bearer x"}

    sid1 = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    sid2 = client.post("/agents/scribe/sessions", headers=H).json()["id"]

    runtime.append_message(app.state.conn, sid1, UserText(text="hello s1"))
    runtime.append_message(app.state.conn, sid2, UserText(text="hello s2"))
    runtime.append_message(app.state.conn, sid1, AssistantText(text="reply s1"))

    r = client.get("/events", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert "next_since_id" in body
    assert "has_more" in body
    # We see events from both sessions interleaved by insertion order:
    sids = [e["session_id"] for e in body["events"]]
    assert sid1 in sids and sid2 in sids
    # Every event has the cross-session metadata we need
    for e in body["events"]:
        assert "id" in e
        assert "agent_name" in e
        assert "created_at" in e
        assert "kind" in e


def test_get_events_since_id_returns_only_newer(ark_home, tmp_path):
    client, app = _make_client(ark_home, tmp_path)
    H = {"Authorization": "Bearer x"}

    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    for i in range(5):
        runtime.append_message(app.state.conn, sid, UserText(text=f"msg-{i}"))

    all_events = client.get("/events", headers=H).json()["events"]
    assert len(all_events) == 5
    mid_id = all_events[2]["id"]

    r = client.get(f"/events?since_id={mid_id}", headers=H)
    body = r.json()
    after = body["events"]
    assert len(after) == 2  # only msg-3 and msg-4
    assert all(e["id"] > mid_id for e in after)
    assert body["next_since_id"] == after[-1]["id"]


def test_get_events_respects_limit_and_reports_has_more(ark_home, tmp_path):
    client, app = _make_client(ark_home, tmp_path)
    H = {"Authorization": "Bearer x"}
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    for i in range(10):
        runtime.append_message(app.state.conn, sid, UserText(text=f"msg-{i}"))

    r = client.get("/events?since_id=0&limit=4", headers=H)
    body = r.json()
    assert len(body["events"]) == 4
    assert body["has_more"] is True


def test_get_events_no_cursor_returns_latest(ark_home, tmp_path):
    client, app = _make_client(ark_home, tmp_path)
    H = {"Authorization": "Bearer x"}
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    for i in range(10):
        runtime.append_message(app.state.conn, sid, UserText(text=f"msg-{i}"))

    r = client.get("/events?limit=3", headers=H)
    body = r.json()
    # No cursor → latest 3, ascending
    assert len(body["events"]) == 3
    texts = [e["data"]["text"] for e in body["events"]]
    assert texts == ["msg-7", "msg-8", "msg-9"]


def test_get_events_translates_injected_message_kind(ark_home, tmp_path):
    """Same translation as /history: AssistantText with injected_from set
    surfaces as kind=InjectedMessage."""
    client, app = _make_client(ark_home, tmp_path)
    H = {"Authorization": "Bearer x"}
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    runtime.append_message(
        app.state.conn, sid, AssistantText(text="cron tick", injected_from="src-xyz")
    )
    events = client.get("/events", headers=H).json()["events"]
    assert events[-1]["kind"] == "InjectedMessage"
    assert events[-1]["data"] == {"text": "cron tick", "from_session_id": "src-xyz"}


def test_get_events_requires_auth(ark_home, tmp_path):
    client, _ = _make_client(ark_home, tmp_path)
    r = client.get("/events")
    assert r.status_code == 401


def test_get_events_includes_metadata_kinds(ark_home, tmp_path):
    """All kinds surface, including TurnMetrics/SessionContext/etc.
    Clients filter what they render."""
    client, app = _make_client(ark_home, tmp_path)
    H = {"Authorization": "Bearer x"}
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    runtime.append_message(app.state.conn, sid, SessionContext(text="be terse"))
    runtime.append_message(app.state.conn, sid, UserText(text="hi"))
    kinds = [e["kind"] for e in client.get("/events", headers=H).json()["events"]]
    assert "SessionContext" in kinds
    assert "UserText" in kinds


# ---------------------------------------------------------------------------
# WS /events — unified per-client firehose
# ---------------------------------------------------------------------------


def test_ws_events_rejects_unauthed(ark_home, tmp_path):
    client, _ = _make_client(ark_home, tmp_path)
    # No token query param, no auth header → should refuse
    with pytest.raises(Exception):
        with client.websocket_connect("/events") as ws:
            ws.receive_json()


def test_ws_events_rejects_user_message_without_session_id(ark_home, tmp_path):
    client, _ = _make_client(ark_home, tmp_path)
    with client.websocket_connect("/events?token=x") as ws:
        ws.send_json({"type": "user_message", "text": "hi"})
        evt = ws.receive_json()
        assert evt["type"] == "error"
        assert "session_id" in evt["message"]


def test_ws_events_rejects_unknown_session(ark_home, tmp_path):
    client, _ = _make_client(ark_home, tmp_path)
    with client.websocket_connect("/events?token=x") as ws:
        ws.send_json({"type": "user_message", "session_id": "no-such-id", "text": "hi"})
        evt = ws.receive_json()
        assert evt["type"] == "error"
        assert "unknown session" in evt["message"]


def test_ws_events_rejects_unsupported_command(ark_home, tmp_path):
    client, _ = _make_client(ark_home, tmp_path)
    with client.websocket_connect("/events?token=x") as ws:
        ws.send_json({"type": "ping"})
        evt = ws.receive_json()
        assert evt["type"] == "error"
        assert "ping" in evt["message"]


@pytest.mark.asyncio
async def test_run_and_publish_tags_events(ark_home, tmp_path):
    """Every event passing through run_and_publish must carry session_id +
    agent_name so global subscribers can route."""
    cfg = make_config(tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    class _StubProvider:
        async def stream_turn(self, **kwargs):
            from ark.types import TextDelta

            yield TextDelta(text="hi")
            yield AssistantTurnEnd(text="hi", stop_reason="end_turn")

    gq = broker.subscribe_all()
    try:
        import ark.runtime as rt_mod

        original = rt_mod.make_provider
        rt_mod.make_provider = lambda *_a, **_k: _StubProvider()
        try:
            await runtime.run_and_publish(
                conn=conn,
                config=cfg,
                agent=cfg.agents["scribe"],
                session_id=sid,
                user_text="hello",
            )
        finally:
            rt_mod.make_provider = original

        events = []
        try:
            while True:
                events.append(await asyncio.wait_for(gq.get(), timeout=0.1))
        except asyncio.TimeoutError:
            pass
        assert events, "expected at least one event published"
        for e in events:
            assert e.get("session_id") == sid
            assert e.get("agent_name") == "scribe"
        # And we should see a `done` event closing out the run
        assert any(e.get("type") == "done" for e in events)
    finally:
        broker.unsubscribe_all(gq)
