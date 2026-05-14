"""Session runtime: persistence, turn loop, tool dispatch."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import AsyncIterator

from . import paths, tools
from .config import AgentConfig, Config
from .provider import AnthropicProvider, Provider
from .types import (
    AssistantText,
    AssistantTurnEnd,
    Message,
    RunEnd,
    RuntimeEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    UserText,
    message_from_row,
    message_to_row,
)


# ---------------------------------------------------------------------------
# Provider construction
# ---------------------------------------------------------------------------


def make_provider(provider_type: str, *, api_key: str, base_url: str | None = None) -> Provider:
    if provider_type == "anthropic":
        return AnthropicProvider(api_key=api_key, base_url=base_url)
    if provider_type == "openai":
        from .provider import OpenAIProvider

        return OpenAIProvider(api_key=api_key, base_url=base_url)
    if provider_type == "openrouter":
        from .provider import OpenRouterProvider

        return OpenRouterProvider(api_key=api_key, base_url=base_url)
    raise ValueError(f"unsupported provider_type: {provider_type}")


# ---------------------------------------------------------------------------
# Per-session in-memory state (e.g. loaded skills)
# ---------------------------------------------------------------------------


_session_loaded_skills: dict[str, set[str]] = {}


def loaded_skills(session_id: str) -> set[str]:
    return _session_loaded_skills.setdefault(session_id, set())


def reset_session_state(session_id: str) -> None:
    _session_loaded_skills.pop(session_id, None)


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def create_session(conn: sqlite3.Connection, agent_name: str, kind: str = "conversational") -> str:
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions(id, agent_name, kind, created_at) VALUES (?,?,?,?)",
        (sid, agent_name, kind, now_ms()),
    )
    return sid


def list_sessions(
    conn: sqlite3.Connection,
    agent_name: str,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT id, agent_name, kind, created_at, ended_at FROM sessions WHERE agent_name = ?"
    params: list = [agent_name]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def session_exists(conn: sqlite3.Connection, session_id: str, agent_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE id = ? AND agent_name = ?",
        (session_id, agent_name),
    ).fetchone()
    return row is not None


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    reset_session_state(session_id)


def load_history(conn: sqlite3.Connection, session_id: str) -> list[Message]:
    rows = conn.execute(
        "SELECT role, content_json FROM messages WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [message_from_row(r["role"], json.loads(r["content_json"])) for r in rows]


def append_message(conn: sqlite3.Connection, session_id: str, msg: Message) -> None:
    role, content = message_to_row(msg)
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO messages(session_id, seq, role, content_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (session_id, next_seq, role, json.dumps(content), now_ms()),
    )


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------


def system_prompt(agent: AgentConfig) -> str:
    """Build the system prompt: the user's session_context.md followed by a
    runtime-injected Environment stanza so the model has concrete grounding for
    file/shell tool calls."""

    ctx_path = paths.agent_dir(agent.name) / "session_context.md"
    body = (
        ctx_path.read_text()
        if ctx_path.exists()
        else f"You are {agent.name}, an agent in the Ark harness."
    )
    env = (
        "\n\n---\n"
        "Environment (managed by the Ark harness, do not invent paths):\n"
        f"- Your name: {agent.name}\n"
        f"- Your workspace directory: {agent.workspace}\n"
        "- File and shell tools (read_file, write_file, list_files, run_command) "
        "operate on real paths on this server. The current working directory for "
        "each tool call is your workspace above. When in doubt about where a file "
        "lives, call list_files first instead of guessing.\n"
        "- Files the user attaches arrive in `uploads/` (relative to your workspace). "
        "Newer uploads of the same name are auto-suffixed (e.g. `report-2.pdf`). "
        "Use `list_uploads` to see what's available, newest first.\n"
        "- To hand a file back to the user, write it anywhere in your workspace "
        "and then call `share_with_client(path)`. The user's client will be "
        "notified and given a download link.\n"
    )
    return body + env


# ---------------------------------------------------------------------------
# Turn loop
# ---------------------------------------------------------------------------


async def run_user_turn(
    *,
    conn: sqlite3.Connection,
    config: Config,
    agent: AgentConfig,
    session_id: str,
    user_text: str,
    provider_factory=make_provider,
    max_iterations: int = 16,
) -> AsyncIterator[RuntimeEvent]:
    """Persist the user message, then drive the model → tools → model loop."""

    append_message(conn, session_id, UserText(text=user_text))

    provider_cfg = config.providers[agent.provider]
    provider = provider_factory(
        provider_cfg.provider_type,
        api_key=provider_cfg.api_key,
        base_url=provider_cfg.base_url,
    )
    system = system_prompt(agent)
    skills_for_session = loaded_skills(session_id)

    last_stop_reason: str | None = None
    for _ in range(max_iterations):
        history = load_history(conn, session_id)
        active = tools.active_schemas(agent, skills_for_session)
        pending_tool_calls: list[ToolCallEvent] = []
        turn_text = ""

        async for evt in provider.stream_turn(
            model=agent.model,
            system=system,
            messages=history,
            tools=active,
        ):
            if isinstance(evt, TextDelta):
                turn_text += evt.text
                yield evt
            elif isinstance(evt, ThinkingDelta):
                yield evt
            elif isinstance(evt, ToolCallEvent):
                pending_tool_calls.append(evt)
                yield evt
            elif isinstance(evt, AssistantTurnEnd):
                last_stop_reason = evt.stop_reason
                if turn_text:
                    append_message(conn, session_id, AssistantText(text=turn_text))
                for tc in pending_tool_calls:
                    append_message(
                        conn,
                        session_id,
                        ToolCall(id=tc.id, name=tc.name, input=tc.input),
                    )
                yield AssistantTurnEnd(text=turn_text, stop_reason=evt.stop_reason)

        if not pending_tool_calls:
            yield RunEnd(stop_reason=last_stop_reason)
            return

        ctx = tools.ToolContext(
            conn=conn,
            config=config,
            agent=agent,
            session_id=session_id,
            cwd=agent.workspace,
            loaded_skills=skills_for_session,
        )
        for tc in pending_tool_calls:
            output, is_error = await tools.execute(tc.name, tc.input, ctx=ctx)
            append_message(
                conn,
                session_id,
                ToolResult(call_id=tc.id, output=output, is_error=is_error),
            )
            yield ToolResultEvent(call_id=tc.id, output=output, is_error=is_error)

    yield RunEnd(stop_reason="max_iterations")
