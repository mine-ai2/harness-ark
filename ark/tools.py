"""Built-in tools available to every agent, plus the tool-execution machinery.

Tools are kwargs-style callables. Anything they need from the runtime (the agent
they're running for, the session id, the DB connection, the live broker) comes
via `current_context()` — a contextvar set by the runtime just before dispatch.
"""

from __future__ import annotations

import asyncio
import fnmatch
import sqlite3
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .types import ToolSchema

if TYPE_CHECKING:
    from .config import AgentConfig, Config


# ---------------------------------------------------------------------------
# Tool context (set by the runtime before dispatch; read by tools that need it)
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    config: "Config"
    agent: "AgentConfig"
    session_id: str
    cwd: Path
    loaded_skills: set[str]  # mutable set of skill names loaded for this session


_context: ContextVar[ToolContext] = ContextVar("ark_tool_context")


def current_context() -> ToolContext:
    return _context.get()


class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


@dataclass
class BuiltinTool:
    schema: ToolSchema
    fn: Callable[..., Any]
    is_async: bool = False


BUILTINS: dict[str, BuiltinTool] = {}


def _register(schema: ToolSchema, fn: Callable[..., Any], is_async: bool = False) -> None:
    BUILTINS[schema.name] = BuiltinTool(schema=schema, fn=fn, is_async=is_async)


# ---------------------------------------------------------------------------
# File / shell tools
# ---------------------------------------------------------------------------


def _read_file(*, path: str) -> str:
    p = Path(path).expanduser()
    try:
        return p.read_text()
    except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
        raise ToolError(str(e))


def _write_file(*, path: str, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} bytes to {p}"


def _list_files(*, path: str = ".", pattern: str | None = None) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise ToolError(f"path not found: {p}")
    if p.is_file():
        return str(p)
    entries = sorted(p.iterdir())
    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e.name, pattern)]
    if not entries:
        return "(empty)"
    return "\n".join(f"{e.name}{'/' if e.is_dir() else ''}" for e in entries)


def _run_command(*, command: str, timeout_seconds: float = 60) -> str:
    timeout = min(float(timeout_seconds), 600)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"command timed out after {timeout}s") from e
    parts = []
    if result.stdout:
        parts.append(f"--- stdout ---\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")
    parts.append(f"exit code: {result.returncode}")
    return "\n".join(parts)


_register(
    ToolSchema(
        name="read_file",
        description="Read the full contents of a file from disk.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    _read_file,
)

_register(
    ToolSchema(
        name="write_file",
        description="Write content to a file (overwrites existing). Creates parent dirs.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    ),
    _write_file,
)

_register(
    ToolSchema(
        name="list_files",
        description="List files and directories at the given path. Optional glob pattern filters by name.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string"},
            },
        },
    ),
    _list_files,
)

_register(
    ToolSchema(
        name="run_command",
        description="Run a shell command and return stdout, stderr, and exit code.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 60},
            },
            "required": ["command"],
        },
    ),
    _run_command,
)


# ---------------------------------------------------------------------------
# Internet: search + URL fetch
# ---------------------------------------------------------------------------


async def _search_web(*, query: str, count: int = 10) -> str:
    ctx = current_context()
    tools_cfg = ctx.config.tools or {}
    search_cfg = tools_cfg.get("search_web") or {}
    provider = (search_cfg.get("provider") or "brave").lower()
    if provider == "brave":
        return await _search_web_brave(tools_cfg, query, count)
    if provider == "tavily":
        return await _search_web_tavily(tools_cfg, query, count)
    raise ToolError(
        f"unknown search_web provider {provider!r}. Configure tools.search_web.provider "
        "to one of: 'brave', 'tavily'."
    )


async def _search_web_brave(tools_cfg: dict, query: str, count: int) -> str:
    import re as _re

    import httpx as _httpx

    cfg = tools_cfg.get("brave_search") or {}
    api_key = cfg.get("api_key")
    if not api_key:
        raise ToolError(
            "search_web (brave) requires `tools.brave_search.api_key` to be set in config.json"
        )
    count = max(1, min(int(count), 20))
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                },
                params={"q": query, "count": count},
            )
    except (_httpx.NetworkError, _httpx.TimeoutException, _httpx.HTTPError) as e:
        raise ToolError(f"brave_search request failed: {type(e).__name__}: {e}")
    if r.status_code >= 400:
        raise ToolError(f"brave_search HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    results = ((data.get("web") or {}).get("results") or [])[:count]
    if not results:
        return "(no results)"
    lines = []
    for item in results:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        desc = _re.sub(r"<[^>]+>", "", item.get("description") or "").strip()
        lines.append(f"- {title}\n  {url}\n  {desc}")
    return "\n".join(lines)


async def _search_web_tavily(tools_cfg: dict, query: str, count: int) -> str:
    import httpx as _httpx

    vendor = tools_cfg.get("tavily") or {}
    api_key = vendor.get("api_key")
    if not api_key:
        raise ToolError(
            "search_web (tavily) requires `tools.tavily.api_key` to be set in config.json"
        )
    search_depth = vendor.get("search_depth", "basic")
    count = max(1, min(int(count), 20))
    body = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": count,
    }
    try:
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.tavily.com/search", json=body)
    except (_httpx.NetworkError, _httpx.TimeoutException, _httpx.HTTPError) as e:
        raise ToolError(f"tavily_search request failed: {type(e).__name__}: {e}")
    if r.status_code >= 400:
        raise ToolError(f"tavily_search HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    results = (data.get("results") or [])[:count]
    if not results:
        return "(no results)"
    lines = []
    for item in results:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        # Tavily's `content` is already cleaned text; truncate for digestibility.
        snippet = (item.get("content") or "").strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + "…"
        lines.append(f"- {title}\n  {url}\n  {snippet}")
    return "\n".join(lines)


async def _fetch_url(*, url: str, timeout_seconds: float = 15) -> str:
    from . import resolvers

    ctx = current_context()
    tools_cfg = ctx.config.tools or {}
    cfg = tools_cfg.get("fetch_url") or {}
    spec = cfg.get("resolver_sequence")
    try:
        # `tools_cfg` doubles as vendor blocks: e.g. `tools.tavily.api_key` is
        # picked up by a `{"provider": "tavily"}` resolver entry that doesn't
        # specify its own api_key. Per-entry config still wins on conflict.
        chain = resolvers.build_chain(spec, vendor_blocks=tools_cfg)
    except resolvers.ResolverConfigError as e:
        raise ToolError(f"fetch_url misconfigured: {e}")
    # Per-resolver timeout; whole-chain budget is bounded since each resolver
    # uses its own AsyncClient timeout, so the worst-case wall clock is
    # roughly len(chain) * timeout.
    result = await resolvers.fetch_with_chain(
        url, chain, timeout=float(timeout_seconds)
    )
    if not result.ok:
        raise ToolError(result.content)
    return result.content


_register(
    ToolSchema(
        name="search_web",
        description="Run an internet search via the Brave Search API. Returns the top results as title/URL/snippet triples — follow up on anything that looks relevant with `fetch_url`.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {
                    "type": "integer",
                    "default": 10,
                    "description": "Number of results to return (1–20).",
                },
            },
            "required": ["query"],
        },
    ),
    _search_web,
    is_async=True,
)

_register(
    ToolSchema(
        name="fetch_url",
        description="Fetch a URL and return its readable content. Tries a configured chain of resolvers (e.g. plain httpx first, then Jina Reader for JS-heavy pages). Returns markdown or extracted text; truncates at 1 MB.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 15},
            },
            "required": ["url"],
        },
    ),
    _fetch_url,
    is_async=True,
)


# ---------------------------------------------------------------------------
# Schedule meta-tools — manage the current agent's heartbeat and crons
# ---------------------------------------------------------------------------


def _set_heartbeat(*, seconds: float | None) -> str:
    ctx = current_context()
    if seconds is None or float(seconds) <= 0:
        ctx.conn.execute(
            "INSERT INTO agent_state(agent_name, heartbeat_seconds) VALUES (?, NULL) "
            "ON CONFLICT(agent_name) DO UPDATE SET heartbeat_seconds = NULL",
            (ctx.agent.name,),
        )
        return f"heartbeat disabled for {ctx.agent.name}"
    s = int(seconds)
    ctx.conn.execute(
        "INSERT INTO agent_state(agent_name, heartbeat_seconds) VALUES (?, ?) "
        "ON CONFLICT(agent_name) DO UPDATE SET heartbeat_seconds = excluded.heartbeat_seconds",
        (ctx.agent.name, s),
    )
    return f"heartbeat set to {s}s for {ctx.agent.name}"


def _add_cron(*, id: str, expr: str, prompt: str) -> str:
    # Validate cron expression
    try:
        from croniter import croniter

        croniter(expr)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"invalid cron expression {expr!r}: {e}")
    ctx = current_context()
    ctx.conn.execute(
        "INSERT INTO crons(agent_name, id, expr, prompt, enabled) VALUES (?,?,?,?,1) "
        "ON CONFLICT(agent_name, id) DO UPDATE SET expr=excluded.expr, prompt=excluded.prompt, enabled=1",
        (ctx.agent.name, id, expr, prompt),
    )
    return f"cron '{id}' set: {expr}"


def _remove_cron(*, id: str) -> str:
    ctx = current_context()
    cur = ctx.conn.execute(
        "DELETE FROM crons WHERE agent_name = ? AND id = ?", (ctx.agent.name, id)
    )
    if cur.rowcount == 0:
        raise ToolError(f"no cron with id '{id}'")
    return f"cron '{id}' removed"


def _list_crons() -> str:
    ctx = current_context()
    rows = ctx.conn.execute(
        "SELECT id, expr, prompt FROM crons WHERE agent_name = ? AND enabled = 1 ORDER BY id",
        (ctx.agent.name,),
    ).fetchall()
    if not rows:
        return "(no crons)"
    return "\n".join(f"{r['id']}: {r['expr']} — {r['prompt'][:60]}" for r in rows)


_register(
    ToolSchema(
        name="set_heartbeat",
        description="Set this agent's heartbeat interval in seconds. Pass null or 0 to disable.",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": ["number", "null"]}},
            "required": ["seconds"],
        },
    ),
    _set_heartbeat,
)

_register(
    ToolSchema(
        name="add_cron",
        description="Create or update a cron entry for this agent. `expr` is a 5-field UNIX cron expression; `prompt` is the starting message when the cron fires.",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "expr": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["id", "expr", "prompt"],
        },
    ),
    _add_cron,
)

_register(
    ToolSchema(
        name="remove_cron",
        description="Remove a cron entry by id.",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    ),
    _remove_cron,
)

_register(
    ToolSchema(
        name="list_crons",
        description="List this agent's cron entries.",
        input_schema={"type": "object", "properties": {}},
    ),
    _list_crons,
)


# ---------------------------------------------------------------------------
# Cross-session messaging
# ---------------------------------------------------------------------------


def _list_my_sessions(*, kind: str | None = None, limit: int = 20) -> str:
    from . import runtime  # local import to avoid cycle

    ctx = current_context()
    rows = runtime.list_sessions(ctx.conn, ctx.agent.name, kind=kind, limit=int(limit))
    if not rows:
        return "(no sessions)"
    out = []
    for r in rows:
        out.append(f"{r['id']}  {r['kind']:14s}  created_at={r['created_at']}")
    return "\n".join(out)


def _post_to_session(*, session_id: str, body: str) -> str:
    from . import broker, runtime
    from .types import AssistantText

    ctx = current_context()
    if not runtime.session_exists(ctx.conn, session_id, ctx.agent.name):
        raise ToolError(
            f"session {session_id} not found, or not owned by agent {ctx.agent.name}"
        )
    msg = AssistantText(text=body, injected_from=ctx.session_id)
    runtime.append_message(ctx.conn, session_id, msg)
    broker.publish(
        session_id,
        {
            "type": "injected_message",
            "from_session_id": ctx.session_id,
            "text": body,
        },
    )
    return f"posted to session {session_id}"


_register(
    ToolSchema(
        name="list_my_sessions",
        description="List sessions owned by the current agent, most recent first.",
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Optional filter: 'conversational', 'heartbeat', or 'cron'."},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    _list_my_sessions,
)

_register(
    ToolSchema(
        name="post_to_session",
        description="Inject a message into another session owned by the same agent. The message is recorded as an assistant turn in the target session's history. Use list_my_sessions to find target session ids.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["session_id", "body"],
        },
    ),
    _post_to_session,
)


# ---------------------------------------------------------------------------
# File transfer to/from the client
# ---------------------------------------------------------------------------


def _list_uploads() -> str:
    from . import workspace as ws

    ctx = current_context()
    uploads = ws.uploads_dir(ctx.agent.workspace)
    if not uploads.is_dir():
        return "(no uploads)"
    entries = sorted(
        (p for p in uploads.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not entries:
        return "(no uploads)"
    lines = []
    for p in entries:
        stat = p.stat()
        lines.append(f"uploads/{p.name}  ({stat.st_size} bytes)")
    return "\n".join(lines)


def _share_with_client(*, path: str, description: str = "") -> str:
    from . import broker, runtime, workspace as ws
    from .types import SharedFile

    ctx = current_context()
    try:
        full = ws.resolve(ctx.agent.workspace, path)
    except ws.WorkspaceError as e:
        raise ToolError(str(e))
    if not full.is_file():
        raise ToolError(f"not a file: {path}")

    rel = ws.relative_to_workspace(ctx.agent.workspace, full)
    size = full.stat().st_size
    runtime.append_message(
        ctx.conn, ctx.session_id, SharedFile(path=rel, description=description, size=size)
    )
    broker.publish(
        ctx.session_id,
        {
            "type": "file_available",
            "path": rel,
            "description": description,
            "size": size,
        },
    )
    return f"shared {rel} with the client ({size} bytes)"


_register(
    ToolSchema(
        name="list_uploads",
        description="List files the user has uploaded into this agent's workspace, newest first. Uploads land in `uploads/<filename>`. If two files were uploaded with the same name, the newer one is suffixed (e.g. `report-2.pdf`).",
        input_schema={"type": "object", "properties": {}},
    ),
    _list_uploads,
)

_register(
    ToolSchema(
        name="share_with_client",
        description="Make a file in this agent's workspace available to the user. The file appears in their UI as a downloadable artifact. `path` is workspace-relative; `description` is an optional short note shown alongside.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["path"],
        },
    ),
    _share_with_client,
)


# ---------------------------------------------------------------------------
# Skill meta-tools
# ---------------------------------------------------------------------------


def _list_skills() -> str:
    from . import skills

    manifest = skills.manifest()
    if not manifest:
        return "(no skills installed)"
    ctx = current_context()
    loaded = ctx.agent.always_loaded_skills + sorted(ctx.loaded_skills)
    lines = []
    for name, desc in manifest:
        marker = " [loaded]" if name in loaded else ""
        lines.append(f"{name}{marker} — {desc}")
    return "\n".join(lines)


def _load_skill(*, name: str) -> str:
    from . import skills

    ctx = current_context()
    skill = skills.get(name)
    if skill is None:
        raise ToolError(f"unknown skill: {name}")
    ctx.loaded_skills.add(name)
    tool_names = ", ".join(t.schema.name for t in skill.tools)
    return f"loaded skill '{name}'. tools available: {tool_names or '(none)'}"


_register(
    ToolSchema(
        name="list_skills",
        description="List skills available to this agent. Each skill is a bundle of tools that can be loaded on demand.",
        input_schema={"type": "object", "properties": {}},
    ),
    _list_skills,
)

_register(
    ToolSchema(
        name="load_skill",
        description="Load a skill, making its tools available for the rest of this session.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
    _load_skill,
)


# ---------------------------------------------------------------------------
# Active-tool resolution + execution
# ---------------------------------------------------------------------------


def active_schemas(agent: "AgentConfig", loaded_skills: set[str]) -> list[ToolSchema]:
    from . import skills

    schemas = [t.schema for t in BUILTINS.values()]
    for skill_name in list(agent.always_loaded_skills) + sorted(loaded_skills):
        skill = skills.get(skill_name)
        if skill is None:
            continue
        schemas.extend(t.schema for t in skill.tools)
    # Deduplicate by name, preserving first occurrence.
    seen: set[str] = set()
    out: list[ToolSchema] = []
    for s in schemas:
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append(s)
    return out


def _resolve(name: str, agent: "AgentConfig", loaded_skills: set[str]):
    from . import skills

    if name in BUILTINS:
        return BUILTINS[name].fn, BUILTINS[name].is_async
    for skill_name in list(agent.always_loaded_skills) + sorted(loaded_skills):
        skill = skills.get(skill_name)
        if skill is None:
            continue
        for t in skill.tools:
            if t.schema.name == name:
                return t.fn, t.is_async
    return None, False


def schemas() -> list[ToolSchema]:
    """Backwards-compatible: schemas of the built-ins only."""
    return [t.schema for t in BUILTINS.values()]


async def execute(name: str, args: dict[str, Any], *, ctx: ToolContext) -> tuple[str, bool]:
    fn, is_async = _resolve(name, ctx.agent, ctx.loaded_skills)
    if fn is None:
        return f"unknown tool: {name}", True

    def runner() -> str:
        import os

        token = _context.set(ctx)
        prev = os.getcwd()
        try:
            os.chdir(ctx.cwd)
        except FileNotFoundError:
            ctx.cwd.mkdir(parents=True, exist_ok=True)
            os.chdir(ctx.cwd)
        try:
            result = fn(**args)
        finally:
            os.chdir(prev)
            _context.reset(token)
        return _to_str(result)

    async def async_runner() -> str:
        token = _context.set(ctx)
        try:
            result = await fn(**args)
        finally:
            _context.reset(token)
        return _to_str(result)

    try:
        if is_async:
            output = await async_runner()
        else:
            output = await asyncio.to_thread(runner)
        return output, False
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return f"invalid arguments: {e}", True
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}", True


def _to_str(result: Any) -> str:
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    import json as _json

    try:
        return _json.dumps(result)
    except (TypeError, ValueError):
        return repr(result)
