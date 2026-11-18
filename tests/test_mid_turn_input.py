"""Mid-turn input: a `user_message` sent while the session's turn is running
is queued INTO that turn (drained after a tool-results batch, before the next
provider call → `user_message_injected`), never a second concurrent turn;
whatever is still queued when the turn ends comes back on
`done.undelivered`.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from ark import broker, db, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.provider import to_anthropic_messages, to_google_contents, to_openai_messages
from ark.server import create_app
from ark.types import (
    AssistantText,
    AssistantTurnEnd,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
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


@pytest.fixture(autouse=True)
def _clean_registries():
    runtime._active_turns.clear()
    runtime._turn_inboxes.clear()
    yield
    runtime._active_turns.clear()
    runtime._turn_inboxes.clear()


async def _drain(queue) -> list[dict]:
    events = []
    try:
        while True:
            events.append(await asyncio.wait_for(queue.get(), timeout=0.1))
    except asyncio.TimeoutError:
        pass
    return events


def _setup(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    cfg = make_config(ws_dir)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    return cfg, conn, sid


# ---------------------------------------------------------------------------
# Runtime: injection between tool iterations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_message_is_injected_between_tool_iterations(
    ark_home, tmp_path, monkeypatch
):
    cfg, conn, sid = _setup(tmp_path)
    seen_messages: list[list] = []

    class _ToolThenAnswer:
        calls = 0

        async def stream_turn(self, *, messages, **kwargs):
            self.calls += 1
            seen_messages.append(list(messages))
            if self.calls == 1:
                # The user types while the model is mid-step.
                assert runtime.enqueue_user_message(sid, "also check last month", "c-1")
                yield ToolCallEvent(id="t1", name="nope", input={})
                yield AssistantTurnEnd(text="", stop_reason="tool_use")
            else:
                yield TextDelta(text="done")
                yield AssistantTurnEnd(text="done", stop_reason="end_turn")

    provider = _ToolThenAnswer()
    monkeypatch.setattr(runtime, "make_provider", lambda *_a, **_k: provider)

    gq = broker.subscribe_all()
    try:
        await runtime.run_and_publish(
            conn=conn, config=cfg, agent=cfg.agents["scribe"],
            session_id=sid, user_text="summarize",
        )
        events = [e for e in await _drain(gq) if e.get("session_id") == sid]
    finally:
        broker.unsubscribe_all(gq)

    types = [e["type"] for e in events]
    injected = [e for e in events if e["type"] == "user_message_injected"]
    assert injected == [
        {
            "type": "user_message_injected",
            "client_message_id": "c-1",
            "text": "also check last month",
            "session_id": sid,
            "agent_name": "scribe",
        }
    ]
    # Delivered AFTER the tool result, BEFORE the next provider turn's text.
    assert types.index("tool_result") < types.index("user_message_injected")
    assert types.index("user_message_injected") < types.index("assistant_delta")
    done = events[-1]
    assert done["type"] == "done" and "undelivered" not in done

    # The second provider call saw the injected text right after the result.
    second = seen_messages[1]
    assert isinstance(second[-2], ToolResult)
    assert second[-1] == UserText(text="also check last month", mid_turn=True)
    # Durable: persisted at its injection point.
    history = runtime.load_history(conn, sid)
    kinds = [type(m).__name__ for m in history]
    injected_row = history[kinds.index("ToolResult") + 1]
    assert injected_row == UserText(text="also check last month", mid_turn=True)
    assert history[0] == UserText(text="summarize")  # turn starts stay unflagged
    assert runtime._active_turns == {} and runtime._turn_inboxes == {}


@pytest.mark.asyncio
async def test_leftover_on_plain_text_end_is_reported_undelivered(
    ark_home, tmp_path, monkeypatch
):
    cfg, conn, sid = _setup(tmp_path)

    class _AnswerOnly:
        async def stream_turn(self, **kwargs):
            assert runtime.enqueue_user_message(sid, "one more thing", "c-2")
            yield TextDelta(text="answer")
            yield AssistantTurnEnd(text="answer", stop_reason="end_turn")

    monkeypatch.setattr(runtime, "make_provider", lambda *_a, **_k: _AnswerOnly())
    gq = broker.subscribe_all()
    try:
        await runtime.run_and_publish(
            conn=conn, config=cfg, agent=cfg.agents["scribe"],
            session_id=sid, user_text="q",
        )
        events = [e for e in await _drain(gq) if e.get("session_id") == sid]
    finally:
        broker.unsubscribe_all(gq)

    assert not [e for e in events if e["type"] == "user_message_injected"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["stop_reason"] == "end_turn"
    assert done["undelivered"] == [{"client_message_id": "c-2", "text": "one more thing"}]
    # Never reached the model's history; inbox cleared; turn closed.
    texts = [m.text for m in runtime.load_history(conn, sid) if isinstance(m, UserText)]
    assert texts == ["q"]
    assert runtime._turn_inboxes == {} and runtime._active_turns == {}
    # After close, the session is idle: enqueue refuses (caller starts a turn).
    assert runtime.enqueue_user_message(sid, "next", "c-3") is False


@pytest.mark.asyncio
async def test_max_iterations_does_not_drain_without_a_following_provider_call(
    ark_home, tmp_path, monkeypatch
):
    cfg, conn, sid = _setup(tmp_path)

    class _AlwaysTool:
        async def stream_turn(self, **kwargs):
            yield ToolCallEvent(id="t1", name="nope", input={})
            yield AssistantTurnEnd(text="", stop_reason="tool_use")

    async def fake_turn():
        runtime._active_turns[sid] = asyncio.current_task()
        assert runtime.enqueue_user_message(sid, "late", "c-4")
        events = []
        async for evt in runtime.run_user_turn(
            conn=conn, config=cfg, agent=cfg.agents["scribe"], session_id=sid,
            user_text="go", provider_factory=lambda *_a, **_k: _AlwaysTool(),
            max_iterations=1,
        ):
            events.append(evt)
        return events

    events = await asyncio.create_task(fake_turn())
    assert type(events[-1]).__name__ == "RunEnd"
    assert events[-1].stop_reason == "max_iterations"
    # Not injected (no provider call followed) — still queued for the
    # terminal frame to report.
    assert runtime._turn_inboxes[sid] == [{"client_message_id": "c-4", "text": "late"}]


# ---------------------------------------------------------------------------
# WS: queued into the running turn, never a second turn; stop → undelivered
# ---------------------------------------------------------------------------


class _HangingProvider:
    """First stream hangs after one delta until cancelled."""

    def __init__(self):
        self.calls = 0

    async def stream_turn(self, **kwargs):
        self.calls += 1
        yield TextDelta(text="working…")
        await asyncio.Event().wait()


def _recv_until(ws, event_type, sid, tries=20):
    for _ in range(tries):
        frame = ws.receive_json()
        if frame.get("type") == event_type and frame.get("session_id") == sid:
            return frame
    raise AssertionError(f"never saw {event_type!r} for {sid!r}")


def test_back_to_back_messages_never_start_a_second_turn_and_stop_reports_undelivered(
    ark_home, tmp_path, monkeypatch
):
    provider = _HangingProvider()
    monkeypatch.setattr(runtime, "make_provider", lambda *_a, **_k: provider)
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    client = TestClient(create_app(make_config(ws_dir)))
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    with client.websocket_connect("/events?token=x") as ws:
        # Sent back to back: the second is read before the first turn's task
        # has run at all — it must still be queued, not spawn a turn.
        ws.send_json({"type": "user_message", "session_id": sid, "text": "go"})
        ws.send_json(
            {"type": "user_message", "session_id": sid, "text": "and this", "client_message_id": "m-2"}
        )
        _recv_until(ws, "assistant_delta", sid)
        ws.send_json(
            {"type": "user_message", "session_id": sid, "text": "and that", "client_message_id": "m-3"}
        )
        ws.send_json({"type": "stop", "session_id": sid})
        done = _recv_until(ws, "done", sid)

    assert provider.calls == 1  # exactly one turn ever started
    assert done["stopped"] is True
    assert done["undelivered"] == [
        {"client_message_id": "m-2", "text": "and this"},
        {"client_message_id": "m-3", "text": "and that"},
    ]
    assert runtime._active_turns == {} and runtime._turn_inboxes == {}


def test_non_string_client_message_id_is_an_error_frame(ark_home, tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    client = TestClient(create_app(make_config(ws_dir)))
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    with client.websocket_connect("/events?token=x") as ws:
        ws.send_json({"type": "user_message", "session_id": sid, "text": "x", "client_message_id": 7})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "client_message_id" in frame["message"]
    assert runtime._active_turns == {}


def test_health_advertises_mid_turn_input(ark_home, tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    client = TestClient(create_app(make_config(ws_dir)))
    body = client.get("/health").json()
    assert body["ok"] is True
    assert "mid_turn_input" in body["features"]


# ---------------------------------------------------------------------------
# Provider folding + elision boundaries
# ---------------------------------------------------------------------------


_INJECTED_HISTORY = [
    UserText(text="q"),
    ToolCall(id="t1", name="read", input={}),
    ToolResult(call_id="t1", output="r1", name="read"),
    UserText(text="also this", mid_turn=True),
]


def test_anthropic_folds_injected_text_into_the_tool_result_turn():
    out = to_anthropic_messages(_INJECTED_HISTORY)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    blocks = out[-1]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[1] == {"type": "text", "text": "also this"}


def test_google_folds_injected_text_into_the_function_response_turn():
    contents = to_google_contents(_INJECTED_HISTORY)
    assert [c.role for c in contents] == ["user", "model", "user"]
    parts = contents[-1].parts
    assert parts[0].function_response is not None
    assert parts[1].text == "also this"


def test_openai_sends_injected_text_as_the_next_user_message():
    out = to_openai_messages("", _INJECTED_HISTORY)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "user"]
    assert out[-1]["content"] == "also this"


def test_elision_does_not_count_injected_text_as_a_turn_boundary():
    big = "x" * 5000
    msgs = [
        UserText(text="turn 1"),
        ToolCall(id="a", name="read", input={}),
        ToolResult(call_id="a", output=big, name="read"),
        AssistantText(text="ok"),
        UserText(text="turn 2"),
        ToolCall(id="b", name="read", input={}),
        ToolResult(call_id="b", output=big, name="read"),
        UserText(text="injected 1", mid_turn=True),
        ToolCall(id="c", name="read", input={}),
        ToolResult(call_id="c", output=big, name="read"),
        UserText(text="injected 2", mid_turn=True),
    ]
    out = runtime.elide_tool_results(msgs, keep_turns=1, elide_over=100, max_bytes=1000)
    results = {m.call_id: m.output for m in out if isinstance(m, ToolResult)}
    # keep_turns=1 protects the WHOLE current turn ("turn 2" onward), not
    # just the tail after the last injection.
    assert results["a"].startswith("[tool result elided")
    assert results["b"] == big and results["c"] == big


def test_cli_prints_delivered_and_undelivered(capsys):
    from ark import cli

    ui = cli._Ui()
    ui.is_tty = False
    cli._handle_event(ui, {"type": "user_message_injected", "text": "also this"})
    cli._handle_event(
        ui, {"type": "done", "undelivered": [{"client_message_id": "m", "text": "later"}]}
    )
    err = capsys.readouterr().err
    assert "delivered mid-turn: also this" in err
    assert "not delivered this turn: later" in err
