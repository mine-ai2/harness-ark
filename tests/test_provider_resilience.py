"""Provider-side halves of mine-capstone#1413: the read timeout reaches the
OpenAI-compatible clients, and argument JSON cut off by the output limit is
reported as `truncated` instead of silently becoming `{}`."""

import json
from types import SimpleNamespace as ns

import httpx
import pytest

from ark import config
from ark.provider import (
    DEFAULT_READ_TIMEOUT_SECONDS,
    OpenAIProvider,
    OpenRouterProvider,
    _parse_tool_arguments,
    _translate_openai_stream,
    provider_timeout,
    translate_stream,
)
from ark.runtime import make_provider
from ark.types import ToolCallEvent


# --- read timeout -----------------------------------------------------------


def test_default_timeout_is_120s_read_with_tight_connect():
    t = provider_timeout()
    assert t == httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    assert DEFAULT_READ_TIMEOUT_SECONDS == 120.0


@pytest.mark.parametrize("cls", [OpenAIProvider, OpenRouterProvider])
def test_clients_carry_the_default_timeout(cls):
    assert cls(api_key="k")._client.timeout == provider_timeout()


@pytest.mark.parametrize("provider_type", ["openai", "openrouter"])
def test_configured_read_timeout_reaches_the_client(provider_type):
    p = make_provider(provider_type, api_key="k", read_timeout_seconds=45)
    assert p._client.timeout == httpx.Timeout(connect=10.0, read=45.0, write=30.0, pool=10.0)


@pytest.mark.parametrize("provider_type", ["anthropic", "google"])
def test_other_adapters_accept_and_ignore_the_knob(provider_type):
    make_provider(provider_type, api_key="k", read_timeout_seconds=45)


def _write(home, provider_extra):
    (home / "config.json").write_text(json.dumps({
        "server": {"auth_secret": "shh"},
        "providers": {
            "or": {"provider_type": "openrouter", "api_key": "k", **provider_extra}
        },
        "agents": {"scribe": {"provider": "or", "model": "m"}},
    }))


def test_config_read_timeout_default_and_override(ark_home):
    _write(ark_home, {})
    assert config.load().providers["or"].read_timeout_seconds == 120.0
    _write(ark_home, {"read_timeout_seconds": 45})
    assert config.load().providers["or"].read_timeout_seconds == 45.0


@pytest.mark.parametrize("bad", [0, -1, "120", True, None])
def test_config_read_timeout_rejects_non_positive_numbers(ark_home, bad):
    _write(ark_home, {"read_timeout_seconds": bad})
    with pytest.raises(config.ConfigError, match="read_timeout_seconds"):
        config.load()


# --- truncated tool-call arguments ------------------------------------------


def test_parse_tool_arguments():
    assert _parse_tool_arguments("") == ({}, False)  # legit no-arg call
    assert _parse_tool_arguments("  ") == ({}, False)
    assert _parse_tool_arguments('{"a": 1}') == ({"a": 1}, False)
    assert _parse_tool_arguments('{"path": "a.txt", "content": "half of a sen') == ({}, True)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


def _tool_chunk(index, call_id, name, arguments, finish_reason=None):
    return ns(
        choices=[
            ns(
                delta=ns(
                    content=None,
                    tool_calls=[ns(index=index, id=call_id, function=ns(name=name, arguments=arguments))],
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _finish(reason):
    return ns(choices=[ns(delta=ns(content=None, tool_calls=None), finish_reason=reason)], usage=None)


async def _tool_calls(chunks):
    return [
        e async for e in _translate_openai_stream(_FakeStream(chunks), model="m")
        if isinstance(e, ToolCallEvent)
    ]


@pytest.mark.asyncio
async def test_openai_stream_cut_mid_arguments_is_truncated():
    calls = await _tool_calls([
        _tool_chunk(0, "c0", "read_file", '{"path": "a.txt"}'),
        _tool_chunk(1, "c1", "write_file", '{"path": "b.txt", "content": "half of a sen'),
        _finish("length"),
    ])
    assert calls == [
        ToolCallEvent(id="c0", name="read_file", input={"path": "a.txt"}),
        ToolCallEvent(id="c1", name="write_file", input={}, truncated=True),
    ]


@pytest.mark.asyncio
async def test_openai_length_stop_before_any_argument_bytes_is_truncated():
    calls = await _tool_calls([
        _tool_chunk(0, "c0", "list_things", ""),
        _tool_chunk(1, "c1", "write_file", ""),
        _finish("length"),
    ])
    # Only the LAST call was being written when the limit hit.
    assert [c.truncated for c in calls] == [False, True]


@pytest.mark.asyncio
async def test_openai_complete_calls_are_never_flagged():
    calls = await _tool_calls([
        _tool_chunk(0, "c0", "list_things", ""),
        _tool_chunk(1, "c1", "read_file", '{"path": "a.txt"}'),
        _finish("tool_calls"),
    ])
    assert [c.truncated for c in calls] == [False, False]
    assert calls[1].input == {"path": "a.txt"}


def test_anthropic_stream_cut_mid_arguments_is_truncated():
    events = [
        ns(type="content_block_start", content_block=ns(type="tool_use", id="t", name="write_file")),
        ns(type="content_block_delta", delta=ns(type="input_json_delta", partial_json='{"path": "b.txt", "cont')),
        ns(type="content_block_stop"),
        ns(type="message_delta", delta=ns(stop_reason="max_tokens")),
        ns(type="message_stop"),
    ]
    calls = [e for e in translate_stream(events) if isinstance(e, ToolCallEvent)]
    assert calls == [ToolCallEvent(id="t", name="write_file", input={}, truncated=True)]


def test_cli_names_an_unavailable_provider(capsys):
    from ark import cli

    ui = cli._Ui()
    ui.is_tty = False
    cli._handle_event(
        ui, {"type": "error", "code": "upstream_unavailable", "message": "Upstream idle timeout exceeded"}
    )
    err = capsys.readouterr().err
    assert "provider unavailable" in err and "Upstream idle timeout exceeded" in err
