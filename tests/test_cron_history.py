"""cron_id tracking on sessions + the cron history / session metadata REST."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from ark import db, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.server import create_app
from ark.types import (
    AssistantText,
    RunError,
    ToolCall,
    UserText,
)


H = {"Authorization": "Bearer x"}


def make_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={
            "scribe": AgentConfig(name="scribe", provider="a", model="m", workspace=ws)
        },
    )


def _client(ark_home, tmp_path):
    return TestClient(create_app(make_config(tmp_path)))


# ---------------------------------------------------------------------------
# create_session writes cron_id
# ---------------------------------------------------------------------------


def test_create_session_persists_cron_id(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "cron", cron_id="my-cron")
    row = conn.execute(
        "SELECT cron_id, kind FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["kind"] == "cron"
    assert row["cron_id"] == "my-cron"


def test_create_session_without_cron_id(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    row = conn.execute(
        "SELECT cron_id FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["cron_id"] is None


# ---------------------------------------------------------------------------
# GET /sessions/{sid} — metadata endpoint
# ---------------------------------------------------------------------------


def test_get_session_metadata_basic(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    r = client.get(f"/sessions/{sid}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["agent_name"] == "scribe"
    assert body["kind"] == "conversational"
    assert body["cron_id"] is None
    assert body["project_id"] is None


def test_get_session_metadata_includes_cron_prompt(ark_home, tmp_path):
    """When the session is a cron fire, the metadata response includes the
    cron's prompt so the transcript viewer can show what triggered it."""
    client = _client(ark_home, tmp_path)
    client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do the morning briefing"},
    )
    sid = runtime.create_session(
        client.app.state.conn, "scribe", "cron", cron_id="morning"
    )
    r = client.get(f"/sessions/{sid}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["cron_id"] == "morning"
    assert body["cron_prompt"] == "do the morning briefing"


def test_get_session_metadata_404(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    assert client.get("/sessions/no-such-id", headers=H).status_code == 404


# ---------------------------------------------------------------------------
# GET /agents/{name}/crons/{cron_id}/sessions — fire history
# ---------------------------------------------------------------------------


def test_cron_fires_empty_list(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.get("/agents/scribe/crons/never-fired/sessions", headers=H)
    assert r.status_code == 200
    assert r.json() == []


def test_cron_fires_summary_uses_post_to_session_body(ark_home, tmp_path):
    """The summary prefers the body of the first post_to_session call —
    that's what cron-style 'send a briefing' agents actually deliver."""
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    sid = runtime.create_session(conn, "scribe", "cron", cron_id="briefing")
    runtime.append_message(conn, sid, UserText(text="(cron prompt)"))
    runtime.append_message(
        conn,
        sid,
        ToolCall(
            id="t1",
            name="post_to_session",
            input={"session_id": "target", "body": "Morning briefing delivered"},
        ),
    )
    runtime.append_message(conn, sid, AssistantText(text="all done"))
    r = client.get("/agents/scribe/crons/briefing/sessions", headers=H)
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["summary"] == "Morning briefing delivered"
    assert rows[0]["had_error"] is False
    assert rows[0]["error_code"] is None


def test_cron_fires_summary_falls_back_to_last_assistant(ark_home, tmp_path):
    """When the cron doesn't post_to_session, fall back to the last
    AssistantText."""
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    sid = runtime.create_session(conn, "scribe", "cron", cron_id="silent")
    runtime.append_message(conn, sid, UserText(text="(cron prompt)"))
    runtime.append_message(conn, sid, AssistantText(text="here's the work"))
    runtime.append_message(conn, sid, AssistantText(text="actually here's the final answer"))
    r = client.get("/agents/scribe/crons/silent/sessions", headers=H)
    rows = r.json()
    assert rows[0]["summary"] == "actually here's the final answer"


def test_cron_fires_no_output_when_empty(ark_home, tmp_path):
    """A cron fire that produced nothing (Gemini safety filter, empty input,
    etc.) shows as '(no output)' — the diagnostic-friendly explicit marker."""
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    sid = runtime.create_session(conn, "scribe", "cron", cron_id="quiet")
    runtime.append_message(conn, sid, UserText(text="(cron prompt)"))
    r = client.get("/agents/scribe/crons/quiet/sessions", headers=H)
    assert r.json()[0]["summary"] == "(no output)"


def test_cron_fires_surface_run_error(ark_home, tmp_path):
    """run_error rows are exposed as had_error + error_code so clients can
    color the row red and skip pulling the full history just to know."""
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    sid = runtime.create_session(conn, "scribe", "cron", cron_id="brokes")
    runtime.append_message(conn, sid, UserText(text="(cron prompt)"))
    runtime.append_message(
        conn, sid, RunError(code="context_too_long", message="too big")
    )
    r = client.get("/agents/scribe/crons/brokes/sessions", headers=H)
    rows = r.json()
    assert rows[0]["had_error"] is True
    assert rows[0]["error_code"] == "context_too_long"


def test_cron_fires_ordered_newest_first(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    sids = []
    for _ in range(3):
        sid = runtime.create_session(conn, "scribe", "cron", cron_id="repeating")
        sids.append(sid)
        time.sleep(0.005)  # tiny pause so created_at differs
    r = client.get("/agents/scribe/crons/repeating/sessions", headers=H)
    returned = [row["session_id"] for row in r.json()]
    assert returned == list(reversed(sids))


def test_cron_fires_respects_limit(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    for _ in range(5):
        runtime.create_session(conn, "scribe", "cron", cron_id="many")
    r = client.get("/agents/scribe/crons/many/sessions?limit=3", headers=H)
    assert len(r.json()) == 3


def test_cron_fires_404_unknown_agent(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.get("/agents/no-such-agent/crons/any/sessions", headers=H)
    assert r.status_code == 404


def test_cron_fires_isolates_by_cron_id(ark_home, tmp_path):
    """Different crons on the same agent must not leak into each other's history."""
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    runtime.create_session(conn, "scribe", "cron", cron_id="a")
    runtime.create_session(conn, "scribe", "cron", cron_id="b")
    assert len(client.get("/agents/scribe/crons/a/sessions", headers=H).json()) == 1
    assert len(client.get("/agents/scribe/crons/b/sessions", headers=H).json()) == 1
