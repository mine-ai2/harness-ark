"""Normalized message, tool, and stream event types.

These are the provider-agnostic shapes the rest of Ark works with. Each
provider adapter is responsible for translating to/from its native format.
"""

from __future__ import annotations

import base64
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
    # Provider-specific opaque bytes that must be echoed back when this call
    # reappears in conversation history. Currently only Gemini 2.5+ thinking
    # models populate this — other providers leave it None.
    thought_signature: bytes | None = None


@dataclass
class ToolResult:
    call_id: str
    output: str
    is_error: bool = False
    name: str = ""  # name of the tool that produced this result (required by Google's API)


@dataclass
class UploadMessage:
    """A file the client uploaded into the agent's workspace.

    Stored as user-side context in conversation history. The actual bytes
    live on disk at <workspace>/<path>; this message is just the record."""

    path: str  # workspace-relative
    original_name: str  # filename before any auto-suffix
    size: int


@dataclass
class SharedFile:
    """A file the agent has shared with the client via `share_with_client`.

    Stored as assistant-side context in conversation history. The bytes live
    on disk at <workspace>/<path>; clients fetch via the download endpoint."""

    path: str  # workspace-relative
    description: str = ""
    size: int = 0


@dataclass
class Project:
    """A shared, user-visible working directory that one or more sessions can be
    bound to.

    Unlike an agent's workspace (which is per-agent and private), a project's
    root is intended to be inspected and edited by clients (file browser /
    upload / edit) and watched for changes that fan out to live subscribers.
    Soft-deletable — `deleted_at` is set on delete, files on disk are not
    touched.
    """

    id: str
    name: str
    root: str            # absolute filesystem path
    description: str = ""
    project_context: str = ""  # appended to system prompt for project sessions
    created_at: int = 0
    deleted_at: int | None = None


@dataclass
class SessionContext:
    """Client-supplied per-session instructions, layered onto the system prompt.

    Append-only: multiple SessionContext messages accumulate over the life of
    the session. They are NOT sent to the LLM as conversation turns — the
    runtime extracts them and appends to the system prompt instead."""

    text: str


@dataclass
class TurnMetrics:
    """Per-turn telemetry: token counts reported by the provider.

    Persisted in session history so total session cost / context fill can be
    computed later. NOT sent to the LLM as a conversation turn — the runtime
    filters these out before passing the message list to the provider."""

    input_tokens: int
    output_tokens: int
    model: str = ""


@dataclass
class RunError:
    """A classified failure during a turn. Persisted so clients (and humans
    looking at the session later) can see what went wrong and where."""

    code: str  # one of: context_too_long, rate_limit, auth, other
    message: str


Message = Union[
    UserText,
    AssistantText,
    ToolCall,
    ToolResult,
    UploadMessage,
    SharedFile,
    SessionContext,
    TurnMetrics,
    RunError,
]


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
    thought_signature: bytes | None = None  # see ToolCall.thought_signature


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


@dataclass
class TurnUsageEvent:
    """Token counts reported by the provider for the just-completed turn.

    Yielded by provider adapters just before AssistantTurnEnd. The runtime
    persists this as a TurnMetrics message and forwards it to the client."""

    input_tokens: int
    output_tokens: int
    model: str = ""
    context_window: int | None = None  # provider's known max, if any


@dataclass
class RunErrorEvent:
    """Classified provider failure, surfaced to clients with an actionable code."""

    code: str  # one of: context_too_long, rate_limit, auth, other
    message: str


ProviderEvent = Union[
    TextDelta, ThinkingDelta, ToolCallEvent, AssistantTurnEnd, TurnUsageEvent
]
RuntimeEvent = Union[
    TextDelta,
    ThinkingDelta,
    ToolCallEvent,
    ToolResultEvent,
    AssistantTurnEnd,
    RunEnd,
    TurnUsageEvent,
    RunErrorEvent,
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
        body: dict[str, Any] = {"id": msg.id, "name": msg.name, "input": msg.input}
        if msg.thought_signature:
            body["thought_signature_b64"] = base64.b64encode(msg.thought_signature).decode("ascii")
        return "tool_call", body
    if isinstance(msg, ToolResult):
        body: dict[str, Any] = {
            "call_id": msg.call_id,
            "output": msg.output,
            "is_error": msg.is_error,
        }
        if msg.name:
            body["name"] = msg.name
        return "tool_result", body
    if isinstance(msg, UploadMessage):
        return "upload", {
            "path": msg.path,
            "original_name": msg.original_name,
            "size": msg.size,
        }
    if isinstance(msg, SharedFile):
        return "shared_file", {
            "path": msg.path,
            "description": msg.description,
            "size": msg.size,
        }
    if isinstance(msg, SessionContext):
        return "session_context", {"text": msg.text}
    if isinstance(msg, TurnMetrics):
        return "turn_metrics", {
            "input_tokens": msg.input_tokens,
            "output_tokens": msg.output_tokens,
            "model": msg.model,
        }
    if isinstance(msg, RunError):
        return "run_error", {"code": msg.code, "message": msg.message}
    raise TypeError(f"unknown message type: {type(msg).__name__}")


def message_from_row(role: str, content: dict[str, Any]) -> Message:
    if role == "user":
        return UserText(text=content["text"])
    if role == "assistant":
        return AssistantText(text=content["text"], injected_from=content.get("injected_from"))
    if role == "tool_call":
        sig_b64 = content.get("thought_signature_b64")
        sig = base64.b64decode(sig_b64) if sig_b64 else None
        return ToolCall(
            id=content["id"],
            name=content["name"],
            input=content["input"],
            thought_signature=sig,
        )
    if role == "tool_result":
        return ToolResult(
            call_id=content["call_id"],
            output=content["output"],
            is_error=content.get("is_error", False),
            name=content.get("name", ""),
        )
    if role == "upload":
        return UploadMessage(
            path=content["path"],
            original_name=content["original_name"],
            size=content["size"],
        )
    if role == "shared_file":
        return SharedFile(
            path=content["path"],
            description=content.get("description", ""),
            size=content.get("size", 0),
        )
    if role == "session_context":
        return SessionContext(text=content["text"])
    if role == "turn_metrics":
        return TurnMetrics(
            input_tokens=int(content.get("input_tokens", 0)),
            output_tokens=int(content.get("output_tokens", 0)),
            model=content.get("model", ""),
        )
    if role == "run_error":
        return RunError(code=content.get("code", "other"), message=content.get("message", ""))
    raise ValueError(f"unknown role: {role}")
