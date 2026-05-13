"""Normalized message, tool, and stream event types.

These are the provider-agnostic shapes the rest of Ark works with. Each
provider adapter is responsible for translating to/from its native format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

# ---------------------------------------------------------------------------
# Conversation messages (one row in the `messages` table = one of these)
# ---------------------------------------------------------------------------


@dataclass
class UserText:
    text: str


@dataclass
class AssistantText:
    text: str
    injected_from: str | None = None  # set when injected from another session via post_to_session


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    output: str
    is_error: bool = False


Message = Union[UserText, AssistantText, ToolCall, ToolResult]


# ---------------------------------------------------------------------------
# Tool schemas (what the LLM sees in its tool list)
# ---------------------------------------------------------------------------


@dataclass
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class ToolCallEvent:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultEvent:
    call_id: str
    output: str
    is_error: bool = False


@dataclass
class AssistantTurnEnd:
    """End of one provider turn (one stream_turn call)."""

    text: str  # full assembled assistant text for this turn
    stop_reason: str | None = None


@dataclass
class RunEnd:
    """End of the whole run loop (no more tool calls — the agent is done)."""

    stop_reason: str | None = None


ProviderEvent = Union[TextDelta, ThinkingDelta, ToolCallEvent, AssistantTurnEnd]
RuntimeEvent = Union[
    TextDelta, ThinkingDelta, ToolCallEvent, ToolResultEvent, AssistantTurnEnd, RunEnd
]


# ---------------------------------------------------------------------------
# JSON serialization for the messages table content_json column
# ---------------------------------------------------------------------------


def message_to_row(msg: Message) -> tuple[str, dict[str, Any]]:
    """Return (role, content_dict) for storage."""
    if isinstance(msg, UserText):
        return "user", {"text": msg.text}
    if isinstance(msg, AssistantText):
        body: dict[str, Any] = {"text": msg.text}
        if msg.injected_from:
            body["injected_from"] = msg.injected_from
        return "assistant", body
    if isinstance(msg, ToolCall):
        return "tool_call", {"id": msg.id, "name": msg.name, "input": msg.input}
    if isinstance(msg, ToolResult):
        return "tool_result", {
            "call_id": msg.call_id,
            "output": msg.output,
            "is_error": msg.is_error,
        }
    raise TypeError(f"unknown message type: {type(msg).__name__}")


def message_from_row(role: str, content: dict[str, Any]) -> Message:
    if role == "user":
        return UserText(text=content["text"])
    if role == "assistant":
        return AssistantText(text=content["text"], injected_from=content.get("injected_from"))
    if role == "tool_call":
        return ToolCall(id=content["id"], name=content["name"], input=content["input"])
    if role == "tool_result":
        return ToolResult(
            call_id=content["call_id"],
            output=content["output"],
            is_error=content.get("is_error", False),
        )
    raise ValueError(f"unknown role: {role}")
