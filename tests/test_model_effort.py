"""Per-session advisory model/effort overrides (mine-capstone#568/#569).

Session-create metadata may carry `model` and `effort`. The runtime resolves
them per turn: a model override wins over the agent's configured model (and
is echoed in usage events/metrics), effort presets only ever RAISE the
configured budgets, and absent/unknown values leave behavior byte-identical
to before — the ignore-if-absent contract MineAI's side (#568) relies on.
"""

import pytest

from ark import db, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.types import AssistantTurnEnd, TurnMetrics, TurnUsageEvent


def make_config(tmp_path, agent_max_tokens=None):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    agent = AgentConfig(
        name="scribe",
        provider="a",
        model="claude-sonnet-4-6",
        workspace=ws,
        max_tokens=agent_max_tokens,
    )
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"scribe": agent},
    )


class _CaptureProvider:
    """Records what the runtime asked for; usage event carries model=None so
    the runtime's fallback echo is what lands in metrics."""

    def __init__(self):
        self.calls = []

    async def stream_turn(self, *, model, system, messages, tools, max_tokens=4096, prompt_caching=False):
        self.calls.append({"model": model, "max_tokens": max_tokens})
        yield TurnUsageEvent(input_tokens=10, output_tokens=5, model=None)
        yield AssistantTurnEnd(text="ok", stop_reason="end_turn")


async def _run(cfg, conn, sid, provider):
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
async def test_model_override_reaches_provider_and_echoes(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(
        conn, "scribe", "conversational", metadata={"model": "moonshotai/kimi-k3"}
    )
    provider = _CaptureProvider()
    events = await _run(cfg, conn, sid, provider)

    assert provider.calls[0]["model"] == "moonshotai/kimi-k3"
    # Effective model echoed on the usage event AND persisted metrics, even
    # when the provider event itself carried no model.
    (usage,) = [e for e in events if isinstance(e, TurnUsageEvent)]
    assert usage.model == "moonshotai/kimi-k3"
    (metrics,) = [
        m for m in runtime.load_history(conn, sid) if isinstance(m, TurnMetrics)
    ]
    assert metrics.model == "moonshotai/kimi-k3"


@pytest.mark.asyncio
async def test_absent_metadata_is_identical_to_today(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    provider = _CaptureProvider()
    events = await _run(cfg, conn, sid, provider)

    assert provider.calls[0] == {"model": "claude-sonnet-4-6", "max_tokens": 4096}
    (usage,) = [e for e in events if isinstance(e, TurnUsageEvent)]
    assert usage.context_window == 200_000  # the agent's own model, its table row


@pytest.mark.asyncio
async def test_effort_high_raises_output_budget(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(
        conn, "scribe", "conversational", metadata={"effort": "high"}
    )
    provider = _CaptureProvider()
    await _run(cfg, conn, sid, provider)
    assert provider.calls[0]["max_tokens"] == 8192
    assert provider.calls[0]["model"] == "claude-sonnet-4-6"  # model untouched


@pytest.mark.asyncio
async def test_effort_never_lowers_a_larger_configured_budget(ark_home, tmp_path):
    cfg = make_config(tmp_path, agent_max_tokens=16384)
    conn = db.init_db()
    sid = runtime.create_session(
        conn, "scribe", "conversational", metadata={"effort": "high"}
    )
    provider = _CaptureProvider()
    await _run(cfg, conn, sid, provider)
    assert provider.calls[0]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_unknown_values_fall_through_to_defaults(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(
        conn, "scribe", "conversational",
        metadata={"model": 42, "effort": "extreme", "notes": "opaque"},
    )
    provider = _CaptureProvider()
    await _run(cfg, conn, sid, provider)
    assert provider.calls[0] == {"model": "claude-sonnet-4-6", "max_tokens": 4096}


@pytest.mark.asyncio
async def test_overridden_model_uses_table_context_window(ark_home, tmp_path):
    """agent.max_context_tokens describes the CONFIGURED model — a session
    override resolves its window from the table instead."""
    cfg = make_config(tmp_path)
    cfg.agents["scribe"].max_context_tokens = 50_000
    conn = db.init_db()
    sid = runtime.create_session(
        conn, "scribe", "conversational", metadata={"model": "gpt-5"}
    )
    provider = _CaptureProvider()
    events = await _run(cfg, conn, sid, provider)
    (usage,) = [e for e in events if isinstance(e, TurnUsageEvent)]
    assert usage.context_window == 400_000  # gpt-5's table row, not 50k


def test_effort_preset_vocabulary_pinned():
    """The accepted contract: medium = baseline, high = deeper budgets."""
    assert set(runtime.EFFORT_PRESETS) == {"medium", "high"}
    assert runtime.EFFORT_PRESETS["medium"] == {}
    assert runtime.EFFORT_PRESETS["high"]["max_tokens"] == 8192
    assert runtime.EFFORT_PRESETS["high"]["max_iterations"] == 32
