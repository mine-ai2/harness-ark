from types import SimpleNamespace

from ark.provider import to_anthropic_messages, to_anthropic_tool, translate_stream
from ark.types import (
    AssistantText,
    AssistantTurnEnd,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolSchema,
    UserText,
)


def evt(**kwargs):
    """Build a SimpleNamespace event mimicking Anthropic's typed stream events."""
    return SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# message folding
# ---------------------------------------------------------------------------


def test_user_message_passes_through():
    out = to_anthropic_messages([UserText(text="hi")])
    assert out == [{"role": "user", "content": "hi"}]


def test_assistant_with_text_and_tool_calls_fold():
    out = to_anthropic_messages(
        [
            UserText(text="please call the tool"),
            AssistantText(text="sure"),
            ToolCall(id="t1", name="read_file", input={"path": "/etc/hosts"}),
        ]
    )
    assert out == [
        {"role": "user", "content": "please call the tool"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "sure"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "read_file",
                    "input": {"path": "/etc/hosts"},
                },
            ],
        },
    ]


def test_tool_results_collapse_into_one_user_message():
    out = to_anthropic_messages(
        [
            ToolResult(call_id="t1", output="one"),
            ToolResult(call_id="t2", output="two", is_error=True),
        ]
    )
    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "one"},
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": "two",
                    "is_error": True,
                },
            ],
        }
    ]


def test_empty_assistant_text_block_is_dropped():
    out = to_anthropic_messages(
        [AssistantText(text=""), ToolCall(id="x", name="n", input={})]
    )
    assert out == [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "x", "name": "n", "input": {}}],
        }
    ]


def test_tool_schema_conversion():
    s = ToolSchema(
        name="read_file",
        description="read",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert to_anthropic_tool(s) == {
        "name": "read_file",
        "description": "read",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }


# ---------------------------------------------------------------------------
# stream translation
# ---------------------------------------------------------------------------


def test_translate_text_and_tool_call():
    events = [
        evt(type="message_start"),
        evt(type="content_block_start", content_block=evt(type="text")),
        evt(type="content_block_delta", delta=evt(type="text_delta", text="Hi")),
        evt(type="content_block_delta", delta=evt(type="text_delta", text=" there")),
        evt(type="content_block_stop"),
        evt(
            type="content_block_start",
            content_block=evt(type="tool_use", id="abc", name="read_file"),
        ),
        evt(type="content_block_delta", delta=evt(type="input_json_delta", partial_json='{"path":')),
        evt(type="content_block_delta", delta=evt(type="input_json_delta", partial_json=' "x.txt"}')),
        evt(type="content_block_stop"),
        evt(type="message_delta", delta=evt(stop_reason="tool_use")),
        evt(type="message_stop"),
    ]
    out = list(translate_stream(events))
    assert out == [
        TextDelta(text="Hi"),
        TextDelta(text=" there"),
        ToolCallEvent(id="abc", name="read_file", input={"path": "x.txt"}),
        AssistantTurnEnd(text="Hi there", stop_reason="tool_use"),
    ]


def test_translate_thinking_delta():
    events = [
        evt(type="content_block_start", content_block=evt(type="thinking")),
        evt(type="content_block_delta", delta=evt(type="thinking_delta", thinking="hmm")),
        evt(type="content_block_stop"),
        evt(type="content_block_start", content_block=evt(type="text")),
        evt(type="content_block_delta", delta=evt(type="text_delta", text="ok")),
        evt(type="content_block_stop"),
        evt(type="message_delta", delta=evt(stop_reason="end_turn")),
        evt(type="message_stop"),
    ]
    out = list(translate_stream(events))
    assert out == [
        ThinkingDelta(text="hmm"),
        TextDelta(text="ok"),
        AssistantTurnEnd(text="ok", stop_reason="end_turn"),
    ]


def test_translate_handles_empty_tool_input():
    events = [
        evt(type="content_block_start", content_block=evt(type="tool_use", id="t", name="n")),
        evt(type="content_block_stop"),
        evt(type="message_delta", delta=evt(stop_reason="tool_use")),
        evt(type="message_stop"),
    ]
    out = list(translate_stream(events))
    assert out == [
        ToolCallEvent(id="t", name="n", input={}),
        AssistantTurnEnd(text="", stop_reason="tool_use"),
    ]
