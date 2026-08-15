"""Session runtime: persistence, turn loop, tool dispatch."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from typing import AsyncIterator

from dataclasses import asdict, is_dataclass

from . import broker, models, paths, projects, tools
from .config import AgentConfig, Config
from .provider import AnthropicProvider, Provider
from .types import (
    AssistantText,
    AssistantTurnEnd,
    Message,
    Project,
    RunEnd,
    RunError,
    RunErrorEvent,
    RuntimeEvent,
    SessionContext,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    TurnMetrics,
    TurnUsageEvent,
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
    if provider_type == "google":
        from .provider import GoogleProvider

        return GoogleProvider(api_key=api_key, base_url=base_url)
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


def create_session(
    conn: sqlite3.Connection,
    agent_name: str,
    kind: str = "conversational",
    project_id: str | None = None,
    cron_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions(id, agent_name, kind, created_at, project_id, cron_id, "
        "metadata_json) VALUES (?,?,?,?,?,?,?)",
        (
            sid,
            agent_name,
            kind,
            now_ms(),
            project_id,
            cron_id,
            json.dumps(metadata) if metadata else None,
        ),
    )
    return sid


def session_metadata(conn: sqlite3.Connection, session_id: str) -> dict:
    """Client-supplied session metadata ({} when none). Server-side only:
    surfaced to skills via ToolContext, never to the model."""

    row = conn.execute(
        "SELECT metadata_json FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or not row["metadata_json"]:
        return {}
    try:
        data = json.loads(row["metadata_json"])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def session_project(conn: sqlite3.Connection, session_id: str) -> Project | None:
    """Return the Project bound to a session, or None if the session is
    project-less (or the project has been deleted)."""

    row = conn.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or row["project_id"] is None:
        return None
    p = projects.get(conn, row["project_id"])
    if p is None or p.deleted_at is not None:
        return None
    return p


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


def system_prompt(
    agent: AgentConfig,
    contexts: list[SessionContext] | None = None,
    project: Project | None = None,
) -> str:
    """Build the system prompt.

    Layers (top to bottom):
      1. The user's `session_context.md` — agent identity / persona
      2. The Environment stanza — runtime facts (workspace path, available
         file/shell/upload helpers)
      3. Project framing — only when this session is bound to a project
      4. Any client-supplied SessionContext messages, concatenated in order
    """

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
        "- Files the user attaches arrive in `uploads/` (relative to your workspace, "
        "or to the project root if this session is in a project — see below). "
        "Newer uploads of the same name are auto-suffixed (e.g. `report-2.pdf`). "
        "Use `list_uploads` to see what's available, newest first.\n"
        "- To hand a file back to the user, write it anywhere in your workspace "
        "and then call `share_with_client(path)`. The user's client will be "
        "notified and given a download link.\n"
    )
    out = body + env
    if project is not None:
        proj = (
            "\n\n---\n"
            "Project (this session):\n"
            f"- Name: {project.name}\n"
            f"- Root: {project.root}\n"
        )
        if project.description:
            proj += f"- Description: {project.description}\n"
        if project.project_context.strip():
            proj += "\n" + project.project_context.strip() + "\n"
        proj += (
            "\nAll file operations should target paths under the project root above "
            "unless explicitly asked to modify your workspace. The project is where "
            "the user can see and edit your work; your workspace is private scratch "
            "space. Uploads in this session land in `<project_root>/uploads/`.\n"
        )
        out += proj
    if contexts:
        joined = "\n\n".join(c.text for c in contexts if c.text.strip())
        if joined:
            out += (
                "\n\n---\n"
                "Session context (provided by the client for this session — "
                "additive, do not override the agent context above):\n"
                + joined
                + "\n"
            )
    return out


def append_context(conn: sqlite3.Connection, session_id: str, text: str) -> int:
    """Append a SessionContext message to a session. Returns the new total."""

    append_message(conn, session_id, SessionContext(text=text))
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'session_context'",
        (session_id,),
    ).fetchone()[0]


def classify_provider_error(exc: Exception) -> tuple[str, str]:
    """Map a provider exception to (code, message) for client surfacing.

    Codes: 'context_too_long' | 'rate_limit' | 'auth' | 'other'. The match is
    intentionally loose — uses both exception class name and message text so
    it works across Anthropic, OpenAI, OpenRouter (OpenAI-shaped), and Google
    without coupling to their import paths.
    """

    name = type(exc).__name__
    raw = str(exc)
    low = raw.lower()
    context_hits = (
        "context_length_exceeded",
        "context length",
        "prompt is too long",
        "input is too long",
        "input token count",
        "exceeds the model's context",
        "exceeds the maximum context",
        "maximum context length",
    )
    if any(k in low for k in context_hits):
        return "context_too_long", raw
    if "RateLimit" in name or "rate limit" in low or "rate_limit" in low or "429" in raw:
        return "rate_limit", raw
    if (
        "Authentication" in name
        or "Unauthorized" in name
        or "401" in raw
        or "invalid api key" in low
        or "invalid_api_key" in low
    ):
        return "auth", raw
    return "other", raw


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
    provider_factory=None,
    max_iterations: int = 16,
) -> AsyncIterator[RuntimeEvent]:
    """Persist the user message, then drive the model → tools → model loop."""

    # Late-bound default: resolve the module attribute at call time so tests
    # can inject a stub via `runtime.make_provider` (the def-time default
    # captured the original function and silently ignored that patch).
    if provider_factory is None:
        provider_factory = make_provider

    append_message(conn, session_id, UserText(text=user_text))

    provider_cfg = config.providers[agent.provider]
    provider = provider_factory(
        provider_cfg.provider_type,
        api_key=provider_cfg.api_key,
        base_url=provider_cfg.base_url,
    )
    skills_for_session = loaded_skills(session_id)
    context_window = models.context_window_for(
        agent.model, agent.max_context_tokens
    )
    project = session_project(conn, session_id)

    last_stop_reason: str | None = None
    # Message kinds that exist as session-history metadata but must NOT be
    # passed to the LLM as conversation turns (system_prompt material,
    # telemetry, or recorded errors).
    _llm_excluded = (SessionContext, TurnMetrics, RunError)
    for _ in range(max_iterations):
        history = load_history(conn, session_id)
        contexts = [m for m in history if isinstance(m, SessionContext)]
        turn_messages = [m for m in history if not isinstance(m, _llm_excluded)]
        system = system_prompt(agent, contexts, project=project)
        active = tools.active_schemas(agent, skills_for_session)
        pending_tool_calls: list[ToolCallEvent] = []
        turn_text = ""

        try:
            async for evt in provider.stream_turn(
                model=agent.model,
                system=system,
                messages=turn_messages,
                tools=active,
                max_tokens=agent.max_tokens or 4096,
            ):
                if isinstance(evt, TextDelta):
                    turn_text += evt.text
                    yield evt
                elif isinstance(evt, ThinkingDelta):
                    yield evt
                elif isinstance(evt, ToolCallEvent):
                    pending_tool_calls.append(evt)
                    yield evt
                elif isinstance(evt, TurnUsageEvent):
                    append_message(
                        conn,
                        session_id,
                        TurnMetrics(
                            input_tokens=evt.input_tokens,
                            output_tokens=evt.output_tokens,
                            model=evt.model or agent.model,
                        ),
                    )
                    # Re-emit with the agent's known context_window so the
                    # client can show a percentage.
                    yield TurnUsageEvent(
                        input_tokens=evt.input_tokens,
                        output_tokens=evt.output_tokens,
                        model=evt.model or agent.model,
                        context_window=context_window,
                    )
                elif isinstance(evt, AssistantTurnEnd):
                    last_stop_reason = evt.stop_reason
                    if turn_text:
                        append_message(conn, session_id, AssistantText(text=turn_text))
                    for tc in pending_tool_calls:
                        append_message(
                            conn,
                            session_id,
                            ToolCall(
                                id=tc.id,
                                name=tc.name,
                                input=tc.input,
                                thought_signature=tc.thought_signature,
                            ),
                        )
                    yield AssistantTurnEnd(text=turn_text, stop_reason=evt.stop_reason)
        except Exception as exc:  # noqa: BLE001
            code, message = classify_provider_error(exc)
            append_message(conn, session_id, RunError(code=code, message=message))
            yield RunErrorEvent(code=code, message=message)
            yield RunEnd(stop_reason=f"error:{code}")
            return

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
            metadata=session_metadata(conn, session_id),
        )
        for tc in pending_tool_calls:
            output, is_error = await tools.execute(tc.name, tc.input, ctx=ctx)
            append_message(
                conn,
                session_id,
                ToolResult(
                    call_id=tc.id, output=output, is_error=is_error, name=tc.name
                ),
            )
            yield ToolResultEvent(call_id=tc.id, output=output, is_error=is_error)

    yield RunEnd(stop_reason="max_iterations")


# ---------------------------------------------------------------------------
# In-flight turn registry (mine-capstone#485)
# ---------------------------------------------------------------------------

# session_id -> the asyncio.Task driving its in-flight turn. Registered by
# run_and_publish itself (asyncio.current_task on entry), so every spawn
# site — the WS handler, the scheduler — gets stop support without changes.
# Ark has no per-session concurrency guard; if a client violates the
# one-turn-at-a-time protocol the newest turn owns the slot.
_active_turns: dict[str, "asyncio.Task"] = {}


def stop_turn(session_id: str) -> bool:
    """Cancel the in-flight turn for a session (the `stop` WS command).

    Returns True when a running turn was cancelled. The cancelled task
    publishes a terminal ``done {"stopped": true}`` itself, so every
    subscriber sees the turn close.
    """

    task = _active_turns.get(session_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


# ---------------------------------------------------------------------------
# Wire-format conversion + broker publishing
# ---------------------------------------------------------------------------


def event_to_wire(evt: RuntimeEvent | Message) -> dict:
    """Convert a RuntimeEvent (or persisted Message) to its wire-format dict.

    Lives in runtime.py rather than server.py so both the WebSocket handler
    and the scheduler can use it without circular imports.
    """

    if isinstance(evt, TextDelta):
        return {"type": "assistant_delta", "text": evt.text}
    if isinstance(evt, ThinkingDelta):
        return {"type": "thinking", "delta": evt.text}
    if isinstance(evt, AssistantTurnEnd):
        return {"type": "assistant_message", "text": evt.text}
    if isinstance(evt, ToolCallEvent):
        return {"type": "tool_call", "id": evt.id, "name": evt.name, "input": evt.input}
    if isinstance(evt, ToolResultEvent):
        return {
            "type": "tool_result",
            "id": evt.call_id,
            "output": evt.output,
            "error": evt.is_error,
        }
    if isinstance(evt, TurnUsageEvent):
        return {
            "type": "turn_usage",
            "input_tokens": evt.input_tokens,
            "output_tokens": evt.output_tokens,
            "model": evt.model,
            "context_window": evt.context_window,
        }
    if isinstance(evt, RunErrorEvent):
        return {"type": "error", "code": evt.code, "message": evt.message}
    if isinstance(evt, RunEnd):
        return {"type": "done", "stop_reason": evt.stop_reason}
    if is_dataclass(evt):
        return {"type": type(evt).__name__, **asdict(evt)}
    return {"type": "unknown"}


async def run_and_publish(
    *,
    conn: sqlite3.Connection,
    config: Config,
    agent: AgentConfig,
    session_id: str,
    user_text: str,
) -> None:
    """Drive a user turn and publish each event to the broker.

    Every event is tagged with `session_id` and `agent_name` so subscribers
    (per-session or global) can route. Use this from any code path that wants
    a turn's events visible to connected clients — the unified WS handler,
    the scheduler, etc.
    """

    task = asyncio.current_task()
    if task is not None:
        _active_turns[session_id] = task
    try:
        async for evt in run_user_turn(
            conn=conn,
            config=config,
            agent=agent,
            session_id=session_id,
            user_text=user_text,
        ):
            wire = event_to_wire(evt)
            wire["session_id"] = session_id
            wire["agent_name"] = agent.name
            broker.publish(session_id, wire)
    except asyncio.CancelledError:
        # Real mid-turn cancel (`stop`, mine-capstone#485): close the turn
        # with a terminal event so clients aren't left mid-stream, then let
        # the cancellation land (the task ends cancelled).
        broker.publish(
            session_id,
            {
                "type": "done",
                "stop_reason": "stopped",
                "stopped": True,
                "session_id": session_id,
                "agent_name": agent.name,
            },
        )
        raise
    except Exception as e:  # noqa: BLE001
        # run_user_turn catches provider exceptions itself; this is for anything
        # that escapes (programming errors, broker failures, etc.).
        broker.publish(
            session_id,
            {
                "type": "error",
                "session_id": session_id,
                "agent_name": agent.name,
                "code": "other",
                "message": f"{type(e).__name__}: {e}",
            },
        )
    finally:
        if task is not None and _active_turns.get(session_id) is task:
            del _active_turns[session_id]
