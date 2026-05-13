"""OpenAI provider: message folding + stream translation."""

from types import SimpleNamespace

import pytest

from ark.provider import _translate_openai_stream, to_openai_messages, to_openai_tool
from ark.types import (
    AssistantText,
    AssistantTurnEnd,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolSchema,
    UserText,
)


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def test_fold_user_message():
    assert to_openai_messages("", [UserText(text="hi")]) == [
        {"role": "user", "content": "hi"}
    ]


def test_fold_system_prepended():
    out = to_openai_messages("be helpful", [UserText(text="hi")])
    assert out[0] == {"role": "system", "content": "be helpful"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_fold_assistant_text_and_tool_call():
    out = to_openai_messages(
        "",
        [
            UserText(text="?"),
            AssistantText(text="sure"),
            ToolCall(id="t1", name="read_file", input={"path": "/x"}),
        ],
    )
    assert out[1] == {
        "role": "assistant",
        "content": "sure",
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "/x"}'},
            }
        ],
    }


def test_fold_tool_result_role():
    out = to_openai_messages("", [ToolResult(call_id="t1", output="contents")])
    assert out == [{"role": "tool", "tool_call_id": "t1", "content": "contents"}]


def test_fold_tool_result_error_prefixed():
    out = to_openai_messages("", [ToolResult(call_id="t1", output="boom", is_error=True)])
    assert out[0]["content"].startswith("ERROR:")


def test_tool_schema_conversion():
    s = ToolSchema(
        name="read_file",
        description="read",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert to_openai_tool(s) == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }


# ---------------------------------------------------------------------------
# Stream translation
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


@pytest.mark.asyncio
async def test_translate_text_and_tool_call_stream():
    chunks = [
        ns(choices=[ns(delta=ns(content="Hi", tool_calls=None), finish_reason=None)]),
        ns(choices=[ns(delta=ns(content=" there", tool_calls=None), finish_reason=None)]),
        ns(
            choices=[
                ns(
                    delta=ns(
                        content=None,
                        tool_calls=[
                            ns(
                                index=0,
                                id="call_1",
                                function=ns(name="read_file", arguments='{"path":'),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ]
        ),
        ns(
            choices=[
                ns(
                    delta=ns(
                        content=None,
                        tool_calls=[
                            ns(
                                index=0,
                                id=None,
                                function=ns(name=None, arguments=' "a.txt"}'),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ]
        ),
        ns(
            choices=[
                ns(
                    delta=ns(content=None, tool_calls=None),
                    finish_reason="tool_calls",
                )
            ]
        ),
    ]
    out = []
    async for evt in _translate_openai_stream(_FakeStream(chunks)):
        out.append(evt)
    assert out == [
        TextDelta(text="Hi"),
        TextDelta(text=" there"),
        ToolCallEvent(id="call_1", name="read_file", input={"path": "a.txt"}),
        AssistantTurnEnd(text="Hi there", stop_reason="tool_calls"),
    ]
