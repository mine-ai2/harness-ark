"""Google (Gemini) provider — folding + stream translation + dispatch."""

from types import SimpleNamespace

import pytest
from google.genai import types as gtypes

from ark.provider import (
    GoogleProvider,
    _translate_google_stream,
    to_google_contents,
    to_google_function_declaration,
)
from ark.runtime import make_provider
from ark.types import (
    AssistantText,
    AssistantTurnEnd,
    SharedFile,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolSchema,
    UploadMessage,
    UserText,
)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_make_provider_dispatches_google():
    p = make_provider("google", api_key="fake")
    assert isinstance(p, GoogleProvider)


# ---------------------------------------------------------------------------
# Function declaration translation
# ---------------------------------------------------------------------------


def test_function_declaration_uses_json_schema():
    s = ToolSchema(
        name="read_file",
        description="read",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    fd = to_google_function_declaration(s)
    assert fd.name == "read_file"
    assert fd.description == "read"
    assert fd.parameters_json_schema == s.input_schema


# ---------------------------------------------------------------------------
# Content folding
# ---------------------------------------------------------------------------


def test_fold_simple_user_text():
    contents = to_google_contents([UserText(text="hi")])
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert len(contents[0].parts) == 1
    assert contents[0].parts[0].text == "hi"


def test_fold_consecutive_user_messages_merge():
    contents = to_google_contents(
        [
            UserText(text="first"),
            UserText(text="second"),
        ]
    )
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert [p.text for p in contents[0].parts] == ["first", "second"]


def test_fold_upload_becomes_user_text_marker():
    contents = to_google_contents(
        [
            UploadMessage(path="uploads/r.pdf", original_name="report.pdf", size=42),
            UserText(text="summarize please"),
        ]
    )
    assert len(contents) == 1
    assert contents[0].role == "user"
    texts = [p.text for p in contents[0].parts]
    assert any("report.pdf" in t for t in texts)
    assert "summarize please" in texts


def test_fold_assistant_role_is_model():
    contents = to_google_contents(
        [
            UserText(text="hi"),
            AssistantText(text="hello there"),
        ]
    )
    assert contents[1].role == "model"
    assert contents[1].parts[0].text == "hello there"


def test_fold_assistant_text_and_tool_call_into_one_content():
    contents = to_google_contents(
        [
            UserText(text="?"),
            AssistantText(text="let me check"),
            ToolCall(id="abc", name="read_file", input={"path": "/x"}),
        ]
    )
    assert contents[1].role == "model"
    assert len(contents[1].parts) == 2
    assert contents[1].parts[0].text == "let me check"
    fc = contents[1].parts[1].function_call
    assert fc.id == "abc"
    assert fc.name == "read_file"
    assert dict(fc.args) == {"path": "/x"}


def test_fold_tool_call_echoes_thought_signature():
    """Gemini 2.5+ thinking models reject calls echoed without their signature."""
    sig = b"opaque thinking bytes"
    contents = to_google_contents(
        [
            UserText(text="hi"),
            ToolCall(
                id="abc", name="fetch_url", input={"url": "x"}, thought_signature=sig
            ),
        ]
    )
    fc_part = contents[1].parts[0]
    assert fc_part.thought_signature == sig


def test_fold_tool_call_without_signature_omits_field():
    """Non-Gemini calls (or pre-2.5 results) carry no signature; the Part
    should simply not have one set."""
    contents = to_google_contents(
        [ToolCall(id="abc", name="fetch_url", input={"url": "x"})]
    )
    fc_part = contents[0].parts[0]
    assert fc_part.thought_signature is None


def test_fold_shared_file_into_model_role():
    contents = to_google_contents(
        [
            AssistantText(text="done"),
            SharedFile(path="chart.png", description="Q4", size=100),
        ]
    )
    assert contents[0].role == "model"
    texts = [p.text for p in contents[0].parts]
    assert "done" in texts
    assert any("chart.png" in t and "Q4" in t for t in texts)


def test_fold_tool_result_into_user_function_response():
    contents = to_google_contents(
        [ToolResult(call_id="abc", output="42", name="read_file")]
    )
    assert len(contents) == 1
    assert contents[0].role == "user"
    fr = contents[0].parts[0].function_response
    assert fr.id == "abc"
    assert fr.name == "read_file"  # Google requires non-empty
    assert dict(fr.response) == {"output": "42"}


def test_fold_tool_result_name_recovered_from_preceding_tool_call():
    """Old rows in DB might lack ToolResult.name. The fold logic must look back
    to the matching ToolCall to get a non-empty name (Google rejects empty)."""

    contents = to_google_contents(
        [
            ToolCall(id="t1", name="fetch_url", input={"url": "https://x"}),
            ToolResult(call_id="t1", output="page body"),  # no name set
        ]
    )
    # contents[0] is the assistant turn (tool_call); contents[1] is the user
    # turn (tool_result).
    fr = contents[1].parts[0].function_response
    assert fr.id == "t1"
    assert fr.name == "fetch_url"


def test_fold_tool_result_no_match_falls_back_to_call_id():
    """If there's no matching ToolCall to look up (unusual but possible),
    use the call_id so the API request still validates."""

    contents = to_google_contents(
        [ToolResult(call_id="orphan", output="x")]  # no preceding ToolCall
    )
    fr = contents[0].parts[0].function_response
    # Must not be empty (Google rejects), so we use call_id as last resort
    assert fr.name == "orphan"


def test_fold_tool_result_error_flagged_in_response_dict():
    contents = to_google_contents(
        [ToolResult(call_id="abc", output="boom", is_error=True)]
    )
    fr = contents[0].parts[0].function_response
    assert dict(fr.response) == {"error": "boom"}


def test_fold_multiple_tool_results_in_one_user_content():
    contents = to_google_contents(
        [
            ToolResult(call_id="a", output="one", name="read_file"),
            ToolResult(call_id="b", output="two", name="write_file"),
        ]
    )
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert len(contents[0].parts) == 2
    assert contents[0].parts[0].function_response.name == "read_file"
    assert contents[0].parts[1].function_response.name == "write_file"


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


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def chunk(parts=(), finish_reason=None):
    """Build a fake GenerateContentResponse chunk."""
    content = ns(parts=list(parts)) if parts else None
    candidate = ns(content=content, finish_reason=finish_reason)
    return ns(candidates=[candidate])


def text_part(text):
    return ns(text=text, function_call=None)


def fc_part(*, name, args, id=None, thought_signature=None):
    return ns(
        text=None,
        function_call=ns(name=name, args=args, id=id),
        thought_signature=thought_signature,
    )


@pytest.mark.asyncio
async def test_translate_text_only_stream():
    chunks = [
        chunk(parts=[text_part("Hi")]),
        chunk(parts=[text_part(" there")]),
        chunk(finish_reason=ns(value="STOP")),
    ]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    assert out == [
        TextDelta(text="Hi"),
        TextDelta(text=" there"),
        AssistantTurnEnd(text="Hi there", stop_reason="stop"),
    ]


@pytest.mark.asyncio
async def test_translate_tool_call_with_id():
    chunks = [
        chunk(
            parts=[fc_part(name="read_file", args={"path": "a.txt"}, id="call-1")],
            finish_reason=ns(value="STOP"),
        ),
    ]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    assert out == [
        ToolCallEvent(id="call-1", name="read_file", input={"path": "a.txt"}),
        AssistantTurnEnd(text="", stop_reason="stop"),
    ]


@pytest.mark.asyncio
async def test_translate_tool_call_without_id_synthesizes_one():
    chunks = [chunk(parts=[fc_part(name="read_file", args={"path": "a.txt"})])]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    tcs = [e for e in out if isinstance(e, ToolCallEvent)]
    assert len(tcs) == 1
    assert tcs[0].id.startswith("gemini_")  # synthesized
    assert tcs[0].name == "read_file"
    assert tcs[0].input == {"path": "a.txt"}


@pytest.mark.asyncio
async def test_translate_captures_thought_signature():
    sig = b"signature-bytes"
    chunks = [
        chunk(
            parts=[fc_part(name="fetch_url", args={"url": "x"}, id="t1", thought_signature=sig)],
            finish_reason=ns(value="STOP"),
        )
    ]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    tcs = [e for e in out if isinstance(e, ToolCallEvent)]
    assert len(tcs) == 1
    assert tcs[0].thought_signature == sig


@pytest.mark.asyncio
async def test_translate_text_and_tool_call_interleaved():
    chunks = [
        chunk(parts=[text_part("checking…")]),
        chunk(
            parts=[fc_part(name="read_file", args={"path": "a"}, id="t1")],
            finish_reason=ns(value="STOP"),
        ),
    ]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    assert out == [
        TextDelta(text="checking…"),
        ToolCallEvent(id="t1", name="read_file", input={"path": "a"}),
        AssistantTurnEnd(text="checking…", stop_reason="stop"),
    ]


@pytest.mark.asyncio
async def test_translate_unspecified_finish_reason_yields_none():
    chunks = [
        chunk(parts=[text_part("ok")]),
        chunk(finish_reason=ns(value="FINISH_REASON_UNSPECIFIED")),
    ]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    end = [e for e in out if isinstance(e, AssistantTurnEnd)][-1]
    assert end.stop_reason is None


@pytest.mark.asyncio
async def test_translate_max_tokens_finish_reason():
    chunks = [
        chunk(parts=[text_part("partial")]),
        chunk(finish_reason=ns(value="MAX_TOKENS")),
    ]
    out = []
    async for evt in _translate_google_stream(_FakeStream(chunks)):
        out.append(evt)
    end = [e for e in out if isinstance(e, AssistantTurnEnd)][-1]
    assert end.stop_reason == "max_tokens"
