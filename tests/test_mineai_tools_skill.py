"""Unit tests for the MineAI gateway proxy skill (mine-capstone#481).

The skill is a deploy/ artifact (agent-scoped, not importable as a package),
so it is loaded from its file path. HTTP is mocked at the httpx module
attribute the skill calls; the runtime ToolContext is faked through the
contextvar the real dispatcher sets.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ark import tools as ark_tools

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "agents" / "talos" / "skills" / "mineai_tools.py"
)


@pytest.fixture()
def skill():
    spec = importlib.util.spec_from_file_location("mineai_tools_under_test", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool_context():
    """Fake the runtime contextvar the dispatcher sets before tool calls."""
    ctx = SimpleNamespace(
        conn=None,
        config=SimpleNamespace(
            tools={"mineai_gateway": {"url": "https://api.example.com/", "secret": "s3cret"}}
        ),
        agent=SimpleNamespace(name="talos"),
        session_id="ark-sess-42",
        cwd=Path("/tmp"),
        loaded_skills=set(),
    )
    token = ark_tools._context.set(ctx)
    yield ctx
    ark_tools._context.reset(token)


class _Response:
    def __init__(self, status_code=200, payload=None, text_body=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text_body

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_skill_exposes_exactly_two_tools(skill):
    from ark.skills import _TOOL_ATTR

    tool_names = [
        name for name in dir(skill)
        if hasattr(getattr(skill, name), _TOOL_ATTR)
    ]
    assert sorted(tool_names) == ["mineai_call_tool", "mineai_list_tools"]
    schema = getattr(skill.mineai_call_tool, _TOOL_ATTR)
    assert schema.input_schema["properties"]["arguments"] == {"type": "object"}
    assert schema.input_schema["required"] == ["name", "arguments"]


def test_list_tools_posts_session_id_with_secret_header(skill, tool_context, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        return _Response(200, {"ok": True, "tools": [{"name": "work.get"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = json.loads(skill.mineai_list_tools())
    assert result == {"ok": True, "tools": [{"name": "work.get"}]}
    assert seen["url"] == "https://api.example.com/api/agent-gateway/list-tools"
    assert seen["body"] == {"session_id": "ark-sess-42"}
    assert seen["headers"] == {"X-Harness-Secret": "s3cret"}


def test_call_tool_passes_name_and_arguments(skill, tool_context, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json)
        return _Response(200, {"ok": True, "result": {"key": "BD-184"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = json.loads(skill.mineai_call_tool(name="work.get", arguments={"id": "abc"}))
    assert result["ok"] is True and result["result"]["key"] == "BD-184"
    assert seen["url"].endswith("/api/agent-gateway/call-tool")
    # mine-capstone#694: one idempotency key per invocation rides the body.
    key = seen["body"].pop("idempotency_key")
    assert isinstance(key, str) and len(key) == 32 and int(key, 16) is not None
    assert seen["body"] == {"session_id": "ark-sess-42", "tool": "work.get", "arguments": {"id": "abc"}}


def test_idempotency_key_stable_across_transport_retry_and_fresh_per_invocation(
    skill, tool_context, monkeypatch
):
    """mine-capstone#694: the 15 s-timeout double-send must carry the SAME
    key (so the gateway replays instead of double-executing), while a new
    invocation mints a new one."""
    bodies = []

    def flaky_post(url, json=None, headers=None, timeout=None):
        bodies.append(json)
        if len(bodies) == 1:
            raise httpx.ConnectError("first attempt dies")
        return _Response(200, {"ok": True, "result": {}})

    monkeypatch.setattr(httpx, "post", flaky_post)
    skill.mineai_call_tool(name="work.update", arguments={"id": "x"})
    assert len(bodies) == 2
    assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"]

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: (bodies.append(k.get("json")), _Response(200, {"ok": True}))[1])
    skill.mineai_call_tool(name="work.update", arguments={"id": "x"})
    assert bodies[2]["idempotency_key"] != bodies[0]["idempotency_key"]


def test_list_tools_optional_narrowing(skill, tool_context, monkeypatch):
    """mine-capstone#692: pack / names ride the body only when given; names
    cap at 20."""
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(body=json)
        return _Response(200, {"ok": True, "tools": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    skill.mineai_list_tools(pack="deals")
    assert seen["body"] == {"session_id": "ark-sess-42", "pack": "deals"}
    skill.mineai_list_tools(names=[f"t{i}" for i in range(25)])
    assert len(seen["body"]["names"]) == 20


def test_structured_denial_unwrapped_from_fastapi_detail(skill, tool_context, monkeypatch):
    denial = {"ok": False, "error": {"code": "tool_not_allowed", "message": "not in tool set"}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(403, {"detail": denial}))
    result = json.loads(skill.mineai_call_tool(name="t.x", arguments={}))
    assert result == denial  # the model reads MineAI's structured denial


def test_transport_failure_retries_once_then_structured_error(skill, tool_context, monkeypatch):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    result = json.loads(skill.mineai_list_tools())
    assert len(calls) == 2  # retry-once
    assert result["ok"] is False
    assert result["error"]["code"] == "gateway_unreachable"


def test_unconfigured_gateway_is_structured(skill, tool_context, monkeypatch):
    tool_context.config = SimpleNamespace(tools={})
    monkeypatch.delenv("MINEAI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("MINEAI_GATEWAY_SECRET", raising=False)
    result = json.loads(skill.mineai_list_tools())
    assert result["error"]["code"] == "gateway_not_configured"


def test_env_fallback_when_config_block_missing(skill, tool_context, monkeypatch):
    tool_context.config = SimpleNamespace(tools={})
    monkeypatch.setenv("MINEAI_GATEWAY_URL", "https://env.example.com")
    monkeypatch.setenv("MINEAI_GATEWAY_SECRET", "env-secret")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _Response(200, {"ok": True, "tools": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    json.loads(skill.mineai_list_tools())
    assert seen["url"].startswith("https://env.example.com/")
    assert seen["headers"]["X-Harness-Secret"] == "env-secret"


# ---------------------------------------------------------------------------
# Per-session metadata resolution (mine-capstone#528)
# ---------------------------------------------------------------------------


def test_metadata_pair_beats_config_and_env(skill, tool_context, monkeypatch):
    tool_context.metadata = {
        "mineai_gateway": {"url": "https://per-session.example/", "secret": "meta-secret"}
    }
    monkeypatch.setenv("MINEAI_GATEWAY_URL", "https://env.example")
    monkeypatch.setenv("MINEAI_GATEWAY_SECRET", "env-secret")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _Response(200, {"ok": True, "tools": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert json.loads(skill.mineai_list_tools())["ok"] is True
    assert seen["url"] == "https://per-session.example/api/agent-gateway/list-tools"
    assert seen["headers"] == {"X-Harness-Secret": "meta-secret"}


def test_incomplete_metadata_pair_never_mixes_with_config(skill, tool_context, monkeypatch):
    """Confused-deputy guard: a session-supplied url without its secret must
    NOT be completed from the deployment's config/env secret."""
    tool_context.metadata = {"mineai_gateway": {"url": "https://rogue.example"}}
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    result = json.loads(skill.mineai_list_tools())
    assert calls == []  # no request left the harness
    assert result["ok"] is False
    assert result["error"]["code"] == "gateway_not_configured"


def test_absent_metadata_falls_back_to_config(skill, tool_context, monkeypatch):
    tool_context.metadata = {}
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _Response(200, {"ok": True, "tools": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert json.loads(skill.mineai_list_tools())["ok"] is True
    assert seen["url"].startswith("https://api.example.com/")
    assert seen["headers"] == {"X-Harness-Secret": "s3cret"}


def test_call_tool_options_passthrough(skill, tool_context, monkeypatch):
    """mine-capstone#696: options rides the body only when given."""
    bodies = []

    def fake_post(url, json=None, headers=None, timeout=None):
        bodies.append(json)
        return _Response(200, {"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    skill.mineai_call_tool(name="documents.read", arguments={"id": "d1"})
    assert "options" not in bodies[0]
    skill.mineai_call_tool(name="documents.read", arguments={"id": "d1"},
                           options={"full": True})
    assert bodies[1]["options"] == {"full": True}
