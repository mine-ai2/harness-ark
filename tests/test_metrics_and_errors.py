"""Per-turn usage metrics + classified error tracking."""

import asyncio

import pytest

from ark import db, models, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.runtime import classify_provider_error
from ark.types import (
    AssistantTurnEnd,
    RunError,
    RunErrorEvent,
    SessionContext,
    TurnMetrics,
    TurnUsageEvent,
    UserText,
    message_from_row,
    message_to_row,
)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_turn_metrics_roundtrip():
    role, body = message_to_row(TurnMetrics(input_tokens=1234, output_tokens=89, model="m"))
    assert role == "turn_metrics"
    assert body == {"input_tokens": 1234, "output_tokens": 89, "model": "m"}
    assert message_from_row(role, body) == TurnMetrics(1234, 89, "m")


def test_run_error_roundtrip():
    role, body = message_to_row(RunError(code="context_too_long", message="boom"))
    assert role == "run_error"
    assert body == {"code": "context_too_long", "message": "boom"}
    assert message_from_row(role, body) == RunError("context_too_long", "boom")


# ---------------------------------------------------------------------------
# Model context-window lookup
# ---------------------------------------------------------------------------


def test_context_window_lookup_known_model():
    assert models.context_window_for("claude-sonnet-4-6") == 200_000
    assert models.context_window_for("gemini-2.5-pro") == 1_048_576


def test_context_window_lookup_unknown():
    assert models.context_window_for("imaginary-model-9000") is None


def test_context_window_override_wins():
    assert models.context_window_for("claude-sonnet-4-6", override=500_000) == 500_000


def test_context_window_strips_openrouter_prefix():
    assert (
        models.context_window_for("anthropic/claude-sonnet-4-6") == 200_000
    )


# ---------------------------------------------------------------------------
# classify_provider_error
# ---------------------------------------------------------------------------


def test_classify_context_too_long_variants():
    for msg in (
        "context_length_exceeded: 200001 > 200000",
        "prompt is too long: 250000 tokens",
        "Input is too long for the model's context window",
        "Request exceeds the maximum context length of 200000 tokens",
    ):
        code, _ = classify_provider_error(ValueError(msg))
        assert code == "context_too_long", msg


def test_classify_rate_limit():
    class RateLimitError(Exception):
        pass

    assert classify_provider_error(RateLimitError("slow down"))[0] == "rate_limit"
    assert classify_provider_error(Exception("429 too many requests"))[0] == "rate_limit"


def test_classify_auth():
    class AuthenticationError(Exception):
        pass

    assert classify_provider_error(AuthenticationError("nope"))[0] == "auth"
    assert classify_provider_error(Exception("401 Unauthorized"))[0] == "auth"
    assert classify_provider_error(Exception("invalid api key"))[0] == "auth"


def test_classify_other_fallback():
    assert classify_provider_error(Exception("server hiccup"))[0] == "other"


# ---------------------------------------------------------------------------
# Runtime: usage is persisted, errors are caught + persisted, both filtered
# from the LLM message list on subsequent iterations.
# ---------------------------------------------------------------------------


def make_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    agent = AgentConfig(
        name="scribe", provider="a", model="claude-sonnet-4-6", workspace=ws
    )
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"scribe": agent},
    )


@pytest.mark.asyncio
async def test_turn_usage_is_persisted_and_emitted(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    class _StubProvider:
        async def stream_turn(self, *, model, system, messages, tools, max_tokens=4096, prompt_caching=False):
            yield TurnUsageEvent(input_tokens=1234, output_tokens=42, model=model)
            yield AssistantTurnEnd(text="ok", stop_reason="end_turn")

    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: _StubProvider(),
    ):
        events.append(evt)

    # Emitted to the client with context_window resolved from the model table:
    usage_events = [e for e in events if isinstance(e, TurnUsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].input_tokens == 1234
    assert usage_events[0].context_window == 200_000  # claude-sonnet-4-6

    # Persisted to history as TurnMetrics:
    history = runtime.load_history(conn, sid)
    metrics = [m for m in history if isinstance(m, TurnMetrics)]
    assert len(metrics) == 1
    assert metrics[0].input_tokens == 1234
    assert metrics[0].output_tokens == 42


@pytest.mark.asyncio
async def test_turn_metrics_filtered_from_llm_list(ark_home, tmp_path):
    """TurnMetrics rows must not be sent to the provider as conversation turns."""
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    runtime.append_message(conn, sid, TurnMetrics(input_tokens=999, output_tokens=10))

    captured = {}

    class _StubProvider:
        async def stream_turn(self, *, model, system, messages, tools, max_tokens=4096, prompt_caching=False):
            captured["messages"] = list(messages)
            yield AssistantTurnEnd(text="ok", stop_reason="end_turn")

    async for _ in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: _StubProvider(),
    ):
        pass

    assert all(not isinstance(m, TurnMetrics) for m in captured["messages"])
    # And the user message is in the list:
    assert any(isinstance(m, UserText) and m.text == "hi" for m in captured["messages"])


@pytest.mark.asyncio
async def test_rate_limit_before_output_retries_then_succeeds(
    ark_home, tmp_path, monkeypatch
):
    """mine-capstone#557 CP2: a 429 at turn start is absorbed with backoff,
    not surfaced — the turn completes as if the throttle never happened."""
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _ThrottledProvider:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("429 too many requests")
            from ark.types import TextDelta

            yield TextDelta(text="finally")
            yield AssistantTurnEnd(text="finally", stop_reason="end_turn")

    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: _ThrottledProvider(),
    ):
        events.append(evt)

    assert calls["n"] == 3
    assert not any(isinstance(e, RunErrorEvent) for e in events)
    ends = [e for e in events if isinstance(e, AssistantTurnEnd)]
    assert ends and ends[0].text == "finally"
    history = runtime.load_history(conn, sid)
    assert not any(isinstance(m, RunError) for m in history)


@pytest.mark.asyncio
async def test_rate_limit_budget_exhausted_surfaces_error(
    ark_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _AlwaysThrottled:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            raise RuntimeError("429 too many requests")
            yield  # unreachable — makes this an async generator

    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: _AlwaysThrottled(),
    ):
        events.append(evt)

    assert calls["n"] == 4  # initial + the full backoff schedule
    errs = [e for e in events if isinstance(e, RunErrorEvent)]
    assert len(errs) == 1 and errs[0].code == "rate_limit"


@pytest.mark.asyncio
async def test_rate_limit_after_output_never_retries(ark_home, tmp_path, monkeypatch):
    """Once anything streamed, a retry would double-emit — fail honestly."""
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _MidStreamThrottle:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            from ark.types import TextDelta

            yield TextDelta(text="partial ")
            raise RuntimeError("429 too many requests")

    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: _MidStreamThrottle(),
    ):
        events.append(evt)

    assert calls["n"] == 1
    errs = [e for e in events if isinstance(e, RunErrorEvent)]
    assert len(errs) == 1 and errs[0].code == "rate_limit"


@pytest.mark.asyncio
async def test_provider_exception_caught_classified_and_persisted(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    class _ExplodingProvider:
        async def stream_turn(self, **_kwargs):
            raise RuntimeError("prompt is too long: 250000 tokens")
            yield  # unreachable but makes this an async generator

    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: _ExplodingProvider(),
    ):
        events.append(evt)

    err_events = [e for e in events if isinstance(e, RunErrorEvent)]
    assert len(err_events) == 1
    assert err_events[0].code == "context_too_long"

    # Persisted in history so subsequent /history reads see it:
    history = runtime.load_history(conn, sid)
    errors = [m for m in history if isinstance(m, RunError)]
    assert len(errors) == 1
    assert errors[0].code == "context_too_long"


# ---------------------------------------------------------------------------
# Provider resilience (mine-capstone#1413): transient upstream failures are
# retried before the first streamed event; a mid-stream failure keeps the
# text already shown; a tool call truncated by the output limit never runs.
# ---------------------------------------------------------------------------


def _named_exc(name: str, message: str = "") -> Exception:
    """An exception whose CLASS NAME is `name` — the classifier matches SDK
    class names without importing them."""
    return type(name, (Exception,), {})(message)


@pytest.mark.parametrize(
    "exc",
    [
        _named_exc("InternalServerError", "Error code: 502 - {'error': {'message': 'Bad Gateway'}}"),
        _named_exc("InternalServerError", "Error code: 500"),
        _named_exc("APIConnectionError", "Connection error."),
        _named_exc("APITimeoutError", "Request timed out."),
        _named_exc("ReadTimeout"),
        _named_exc("ConnectTimeout"),
        _named_exc("RemoteProtocolError", "peer closed connection without sending complete message body"),
        _named_exc("OverloadedError", "Overloaded"),
        _named_exc("ServerError", "503 UNAVAILABLE"),
        TimeoutError(),
        # The two production failures (OpenRouter mid-stream error envelopes):
        _named_exc("APIError", "Upstream idle timeout exceeded"),
        _named_exc("APIError", "Provider returned error"),
        Exception('{"error": {"message": "Provider returned error", "code": 502}}'),
        Exception("Error code: 504 - Gateway Timeout"),
        Exception("Error code: 408 - Request Timeout"),
    ],
)
def test_classify_upstream_unavailable(exc):
    assert classify_provider_error(exc)[0] == "upstream_unavailable"


@pytest.mark.parametrize(
    "exc, code",
    [
        # A 4xx inside OpenRouter's "Provider returned error" envelope is OUR
        # request — never retried as an upstream blip.
        (_named_exc("BadRequestError", "Error code: 400 - {'error': {'message': 'Provider returned error', 'code': 400}}"), "bad_request"),
        (Exception("Error code: 404 - {'error': {'message': 'Provider returned error'}}"), "other"),
        (Exception("Error code: 422 - upstream rejected the tool schema"), "other"),
        # Specific codes keep winning over the new class.
        (Exception("Error code: 429 - upstream is overloaded, rate limit exceeded"), "rate_limit"),
        (Exception("401 Unauthorized — upstream"), "auth"),
        (Exception("prompt is too long: 500000 tokens"), "context_too_long"),
        # No status, no known phrase, no known class → still "other".
        (Exception("server hiccup"), "other"),
        (Exception("see https://example.com:5000/x (port 500)"), "other"),
        (ValueError("unsupported provider_type: nope"), "other"),
    ],
)
def test_classify_upstream_unavailable_does_not_swallow_other_codes(exc, code):
    assert classify_provider_error(exc)[0] == code


async def _collect(cfg, conn, sid, provider):
    events = []
    async for evt in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="hi",
        provider_factory=lambda *_a, **_k: provider,
    ):
        events.append(evt)
    return events


@pytest.mark.asyncio
async def test_502_twice_then_stream_succeeds_with_no_error(ark_home, tmp_path, monkeypatch):
    """Acceptance: a stub provider raising a 502 twice then streaming →
    success, no RunError row, no error event."""
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(runtime.asyncio, "sleep", _fake_sleep)
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _FlakyUpstream:
        async def stream_turn(self, **_kwargs):
            from ark.types import TextDelta

            calls["n"] += 1
            if calls["n"] <= 2:
                raise _named_exc("InternalServerError", "Error code: 502 - Bad Gateway")
            yield TextDelta(text="finally")
            yield AssistantTurnEnd(text="finally", stop_reason="end_turn")

    events = await _collect(cfg, conn, sid, _FlakyUpstream())

    assert calls["n"] == 3
    assert sleeps == [2.0, 5.0]  # the rate-limit schedule, shared
    assert not any(isinstance(e, RunErrorEvent) for e in events)
    assert events[-1].stop_reason == "end_turn"
    history = runtime.load_history(conn, sid)
    assert not any(isinstance(m, RunError) for m in history)
    from ark.types import AssistantText

    assert [m.text for m in history if isinstance(m, AssistantText)] == ["finally"]


@pytest.mark.asyncio
async def test_idle_timeout_before_first_byte_is_retried(ark_home, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _IdleOnce:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _named_exc("APIError", "Upstream idle timeout exceeded")
            yield AssistantTurnEnd(text="", stop_reason="end_turn")

    events = await _collect(cfg, conn, sid, _IdleOnce())
    assert calls["n"] == 2
    assert not any(isinstance(e, RunErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_upstream_unavailable_budget_exhausted_surfaces_error(
    ark_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _AlwaysDown:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            raise _named_exc("APIConnectionError", "Connection error.")
            yield  # unreachable — makes this an async generator

    events = await _collect(cfg, conn, sid, _AlwaysDown())
    assert calls["n"] == 4  # initial + the full backoff schedule
    errs = [e for e in events if isinstance(e, RunErrorEvent)]
    assert len(errs) == 1 and errs[0].code == "upstream_unavailable"
    assert events[-1].stop_reason == "error:upstream_unavailable"


@pytest.mark.asyncio
async def test_non_transient_errors_are_never_retried(ark_home, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _Rejected:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            raise _named_exc("BadRequestError", "Error code: 400 - Provider returned error")
            yield  # unreachable

    events = await _collect(cfg, conn, sid, _Rejected())
    assert calls["n"] == 1
    assert [e.code for e in events if isinstance(e, RunErrorEvent)] == ["bad_request"]


@pytest.mark.asyncio
async def test_mid_stream_failure_keeps_partial_text_before_the_error_row(
    ark_home, tmp_path, monkeypatch
):
    """Acceptance: a mid-stream exception after "Hel" → history holds
    AssistantText("Hel") THEN RunError; never retried (a replayed stream
    would double-emit)."""
    monkeypatch.setattr(runtime, "RATE_LIMIT_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    from ark.types import AssistantText, TextDelta

    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    calls = {"n": 0}

    class _DropsMidStream:
        async def stream_turn(self, **_kwargs):
            calls["n"] += 1
            yield TextDelta(text="Hel")
            raise _named_exc("APIError", "Upstream idle timeout exceeded")

    events = await _collect(cfg, conn, sid, _DropsMidStream())

    assert calls["n"] == 1
    assert [type(e).__name__ for e in events] == ["TextDelta", "RunErrorEvent", "RunEnd"]
    assert events[1].code == "upstream_unavailable"
    tail = [
        m for m in runtime.load_history(conn, sid)
        if isinstance(m, (AssistantText, RunError))
    ]
    assert tail[0] == AssistantText(text="Hel")
    assert isinstance(tail[1], RunError) and tail[1].code == "upstream_unavailable"
    assert len(tail) == 2


@pytest.mark.asyncio
async def test_mid_stream_failure_text_reaches_the_next_turn(ark_home, tmp_path):
    """The saved partial is real conversation history: the next turn's
    provider call sees it (the RunError row stays filtered out)."""
    from ark.types import AssistantText, TextDelta

    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    seen = []

    class _DropThenAnswer:
        def __init__(self):
            self.calls = 0

        async def stream_turn(self, *, messages, **_kwargs):
            self.calls += 1
            seen.append(list(messages))
            if self.calls == 1:
                yield TextDelta(text="Hel")
                raise RuntimeError("socket closed")
            yield TextDelta(text="lo again")
            yield AssistantTurnEnd(text="lo again", stop_reason="end_turn")

    provider = _DropThenAnswer()
    await _collect(cfg, conn, sid, provider)
    await _collect(cfg, conn, sid, provider)
    second = seen[1]
    assert AssistantText(text="Hel") in second
    assert not any(isinstance(m, RunError) for m in second)


@pytest.mark.asyncio
async def test_failure_after_turn_end_does_not_save_the_text_twice(ark_home, tmp_path):
    from ark.types import AssistantText, TextDelta

    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    class _FailsOnClose:
        async def stream_turn(self, **_kwargs):
            yield TextDelta(text="whole answer")
            yield AssistantTurnEnd(text="whole answer", stop_reason="end_turn")
            raise RuntimeError("stream close failed")

    await _collect(cfg, conn, sid, _FailsOnClose())
    texts = [m.text for m in runtime.load_history(conn, sid) if isinstance(m, AssistantText)]
    assert texts == ["whole answer"]


@pytest.mark.asyncio
async def test_truncated_tool_call_is_not_executed(ark_home, tmp_path, monkeypatch):
    """Acceptance: truncated args → tool not executed; the error result
    names truncation; the model gets one more call to retry smaller."""
    from ark import tools
    from ark.types import ToolCall, ToolCallEvent, ToolResult, ToolResultEvent

    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    executed = []

    async def _fake_execute(name, args, *, ctx):
        executed.append((name, args))
        return "ran", False

    monkeypatch.setattr(tools, "execute", _fake_execute)

    class _TruncatedThenDone:
        def __init__(self):
            self.calls = 0

        async def stream_turn(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallEvent(id="ok", name="read_file", input={"path": "a"})
                yield ToolCallEvent(id="cut", name="write_file", input={}, truncated=True)
                yield AssistantTurnEnd(text="", stop_reason="length")
            else:
                yield AssistantTurnEnd(text="", stop_reason="end_turn")

    events = await _collect(cfg, conn, sid, _TruncatedThenDone())

    assert executed == [("read_file", {"path": "a"})]  # the cut call never ran
    results = {e.call_id: e for e in events if isinstance(e, ToolResultEvent)}
    assert results["ok"].is_error is False and results["ok"].output == "ran"
    assert results["cut"].is_error is True
    assert "arguments truncated by the output limit" in results["cut"].output
    assert "retry with a smaller call" in results["cut"].output
    history = runtime.load_history(conn, sid)
    # The call/result pair is still well-formed history for the next call.
    assert ToolCall(id="cut", name="write_file", input={}) in history
    (cut_row,) = [m for m in history if isinstance(m, ToolResult) and m.call_id == "cut"]
    assert cut_row.is_error and cut_row.name == "write_file"
    assert cut_row.output == runtime.TRUNCATED_TOOL_CALL_MESSAGE
    # Wire: additive `truncated` only on the refused call.
    wires = [runtime.event_to_wire(e) for e in events if isinstance(e, ToolCallEvent)]
    assert "truncated" not in wires[0] and wires[1]["truncated"] is True


def test_configured_read_timeout_reaches_the_provider_factory(ark_home, tmp_path):
    """run_user_turn hands providers.<name>.read_timeout_seconds to the
    factory (the client-side half is in tests/test_provider_resilience.py)."""
    cfg = make_config(tmp_path)
    cfg.providers["a"].read_timeout_seconds = 45.0
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    seen = {}

    class _Done:
        async def stream_turn(self, **_kwargs):
            yield AssistantTurnEnd(text="", stop_reason="end_turn")

    def _factory(provider_type, **kwargs):
        seen.update(kwargs, provider_type=provider_type)
        return _Done()

    async def _go():
        async for _ in runtime.run_user_turn(
            conn=conn, config=cfg, agent=cfg.agents["scribe"], session_id=sid,
            user_text="hi", provider_factory=_factory,
        ):
            pass

    asyncio.run(_go())
    assert seen["read_timeout_seconds"] == 45.0
