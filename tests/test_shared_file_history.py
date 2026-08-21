"""share_with_client must not poison the provider conversation.

Regression for the mine-capstone#602 follow-up: `_share_with_client` appends
its SharedFile row from inside `tools.execute`, so it lands BETWEEN the
assistant's ToolCall and the ToolResult the turn loop appends after it. The
provider folders group a contiguous (AssistantText|ToolCall|SharedFile) run
into one assistant message, so the second and later SharedFile of a turn
opened a NEW assistant message between the tool_calls and their results —
emitting a `tool` message with no preceding assistant tool call.

Observed in production twice, on two different providers: Kimi K3 returned
400 "tool messages need a tool/name or a preceding assistant tool call", and
Gemini "The model request was rejected". History is durable, so once a turn
did this every later turn re-sent the malformed array and the session could
never recover.
"""

import json

import pytest

from ark import db, runtime
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.provider import to_anthropic_messages, to_openai_messages
from ark.types import (
    AssistantText,
    AssistantTurnEnd,
    SharedFile,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    UserText,
)


def _turn_history(shares: int):
    """The exact message order a turn with `shares` share_with_client calls
    persists: all ToolCalls, then per call a SharedFile (written by the tool)
    followed by its ToolResult (written by the turn loop)."""
    history = [UserText(text="build the schedule")]
    history += [
        ToolCall(id=f"share_{i}", name="share_with_client", input={"path": f"f{i}.md"})
        for i in range(shares)
    ]
    for i in range(shares):
        history.append(SharedFile(path=f"f{i}.md", description="", size=10))
        history.append(
            ToolResult(
                call_id=f"share_{i}",
                output=f"shared f{i}.md with the client (10 bytes)",
                is_error=False,
                name="share_with_client",
            )
        )
    return history


def _unpaired_openai(messages) -> list[str]:
    """tool messages that do not belong to the assistant tool_calls message
    opening their run — what strict providers reject.

    A run of consecutive `tool` messages after one assistant message is
    legal (that is how parallel calls are answered), so the owner is found
    by walking back PAST sibling tool messages, not just one step.
    """
    bad = []
    for i, m in enumerate(messages):
        if m["role"] != "tool":
            continue
        j = i - 1
        while j >= 0 and messages[j]["role"] == "tool":
            j -= 1
        owner = messages[j] if j >= 0 else {"role": None}
        ids = {t["id"] for t in (owner.get("tool_calls") or [])}
        if owner["role"] != "assistant" or m["tool_call_id"] not in ids:
            bad.append(m["tool_call_id"])
    return bad


def _unpaired_anthropic(messages) -> list[str]:
    """tool_use ids whose tool_result block is not in the very next message."""
    bad = []
    for i, m in enumerate(messages):
        if m["role"] != "assistant" or not isinstance(m["content"], list):
            continue
        uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
        if not uses:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else {"content": []}
        results = {
            b.get("tool_use_id")
            for b in (nxt["content"] if isinstance(nxt.get("content"), list) else [])
            if b.get("type") == "tool_result"
        }
        bad += [u for u in uses if u not in results]
    return bad


# ---------------------------------------------------------------------------
# The runtime keeps SharedFile out of the LLM conversation
# ---------------------------------------------------------------------------


class _ShareTwiceThenStop:
    """Calls share_with_client twice in one turn, then ends — the shape that
    broke production. Records the messages it was asked to send."""

    def __init__(self):
        self.turns = 0
        self.sent: list[list] = []

    async def stream_turn(self, *, model, system, messages, tools, max_tokens=4096):
        self.turns += 1
        self.sent.append(list(messages))
        if self.turns == 1:
            yield ToolCallEvent(id="s0", name="write_file", input={"path": "a.md", "content": "x"})
            yield ToolCallEvent(id="s1", name="write_file", input={"path": "b.md", "content": "y"})
            yield AssistantTurnEnd(text="", stop_reason="tool_use")
        elif self.turns == 2:
            yield ToolCallEvent(id="s2", name="share_with_client", input={"path": "a.md"})
            yield ToolCallEvent(id="s3", name="share_with_client", input={"path": "b.md"})
            yield AssistantTurnEnd(text="", stop_reason="tool_use")
        else:
            yield AssistantTurnEnd(text="done", stop_reason="end_turn")


def _config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    agent = AgentConfig(name="scribe", provider="a", model="m", workspace=ws)
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"scribe": agent},
    )


@pytest.mark.asyncio
async def test_two_shares_in_one_turn_leave_the_history_sendable(ark_home, tmp_path):
    cfg = _config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    provider = _ShareTwiceThenStop()

    async for _ in runtime.run_user_turn(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        user_text="share both files",
        provider_factory=lambda *_a, **_k: provider,
    ):
        pass

    # The SharedFile rows are persisted (the client needs them)...
    history = runtime.load_history(conn, sid)
    assert len([m for m in history if isinstance(m, SharedFile)]) == 2
    # ...but never reach the model.
    final = provider.sent[-1]
    assert not [m for m in final if isinstance(m, SharedFile)]
    # And the conversation the third turn sent is well-formed for both shapes.
    assert _unpaired_openai(to_openai_messages("sys", final)) == []
    assert _unpaired_anthropic(to_anthropic_messages(final)) == []


# ---------------------------------------------------------------------------
# The folding itself, at the shapes that broke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shares", [1, 2, 3, 4])
def test_shared_file_never_splits_a_tool_call_cluster(shares):
    """With SharedFile filtered out (what the runtime now sends), every
    tool result still sits against its own tool call — at any share count."""
    history = [m for m in _turn_history(shares) if not isinstance(m, SharedFile)]
    assert _unpaired_openai(to_openai_messages("sys", history)) == []
    assert _unpaired_anthropic(to_anthropic_messages(history)) == []


@pytest.mark.parametrize("shares", [2, 3, 4])
def test_the_old_shape_is_the_one_that_was_broken(shares):
    """Pins the bug itself, so a future change that re-admits SharedFile to
    the conversation fails loudly here instead of in production."""
    assert len(_unpaired_openai(to_openai_messages("sys", _turn_history(shares)))) == shares - 1


def test_a_single_share_was_always_fine():
    """Why this hid for so long: one share per turn folds into the tool_calls
    cluster and pairs correctly. Only the second one splits it."""
    assert _unpaired_openai(to_openai_messages("sys", _turn_history(1))) == []


def test_assistant_text_between_calls_and_results_is_still_safe():
    """AssistantText clusters the same way; the guard must not have traded
    one interleaving bug for another."""
    history = [
        UserText(text="go"),
        ToolCall(id="c0", name="t", input={}),
        ToolCall(id="c1", name="t", input={}),
        AssistantText(text="working on it"),
        ToolResult(call_id="c0", output="a", is_error=False, name="t"),
        ToolResult(call_id="c1", output="b", is_error=False, name="t"),
    ]
    assert _unpaired_openai(to_openai_messages("sys", history)) == []


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestClassifyProviderError:
    def test_a_malformed_conversation_is_bad_request_not_other(self):
        """The production failure: retrying identical input can never help,
        so it must not share a bucket with transient 'other'."""
        exc = Exception(
            'Error code: 400 - {"message":"Kimi K3 tool messages need a tool/name '
            'or a preceding assistant tool call","type":"Bad Request","code":400}'
        )
        code, message = runtime.classify_provider_error(exc)
        assert code == "bad_request"
        # The human sentence, not the JSON blob.
        assert message == (
            "Kimi K3 tool messages need a tool/name or a preceding assistant tool call"
        )

    def test_googles_wording_also_lands_in_bad_request(self):
        code, _ = runtime.classify_provider_error(
            Exception("The model request was rejected. Check the request and try again.")
        )
        assert code == "bad_request"

    def test_single_quoted_python_repr_bodies_parse(self):
        code, message = runtime.classify_provider_error(
            Exception("Error code: 400 - {'error': {'message': 'unsupported parameter'}}")
        )
        assert code == "bad_request"
        assert message == "unsupported parameter"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Error code: 429 - {'error': {'message': 'Rate limit exceeded.'}}", "rate_limit"),
            ("Error code: 401 - invalid api key", "auth"),
            ("prompt is too long: 300000 tokens", "context_too_long"),
            ("connection reset by peer", "other"),
        ],
    )
    def test_existing_buckets_keep_their_codes(self, raw, expected):
        assert runtime.classify_provider_error(Exception(raw))[0] == expected

    def test_a_rate_limit_is_never_reclassified_as_bad_request(self):
        """429 bodies mention 'request' constantly — order matters."""
        exc = Exception(
            'Error code: 429 - {"error":{"message":"Rate limit exceeded. '
            'Your request was rejected.","code":429}}'
        )
        assert runtime.classify_provider_error(exc)[0] == "rate_limit"

    def test_unparseable_text_is_returned_verbatim(self):
        raw = "something went wrong {not json"
        assert runtime.classify_provider_error(Exception(raw)) == ("other", raw)
