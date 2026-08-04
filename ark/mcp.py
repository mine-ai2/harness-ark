"""MCP (Model Context Protocol) client integration.

Connects to configured MCP servers at Ark boot, caches their tool schemas,
and dispatches tool calls. Servers appear to agents as skills — same
list_skills / load_skill affordances, same tool-name namespace (prefixed
with `<server>__` to prevent collisions).

The `mcp` SDK is imported lazily inside `_open_connection` so the rest of
the module — schema translation, dispatch routing, the manager singleton —
works even on Python versions the SDK doesn't support. Tests substitute a
stub connection factory to exercise the whole pipeline without a real
subprocess.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .types import ToolSchema

# Separator between server name and tool name in the namespaced tool id.
# Double underscore because dots break some provider tool-name validators.
NAMESPACE_SEP = "__"


class MCPError(Exception):
    pass


@dataclass
class MCPServer:
    """One connected MCP server. `tools` is the cached list, namespaced.

    `raw_tool_names` maps namespaced_name → the server-side tool name so
    dispatch doesn't have to re-split.
    """

    name: str
    description: str = ""
    tools: list[ToolSchema] = field(default_factory=list)
    raw_tool_names: dict[str, str] = field(default_factory=dict)
    # Opaque connection object (real: mcp.ClientSession; test: stub).
    connection: Any = None
    exit_stack: AsyncExitStack | None = None
    error: str | None = None  # non-None when the server failed to connect


# ---------------------------------------------------------------------------
# Manager (module-level singleton, mirroring file_watcher's pattern)
# ---------------------------------------------------------------------------


ConnectionFactory = Callable[["MCPServerConfigLike"], Awaitable[tuple[Any, AsyncExitStack, str, list[Any]]]]


class MCPServerConfigLike:
    """Structural stand-in for ark.config.MCPServerConfig — avoids the
    circular import while keeping the signature legible."""


class MCPManager:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}
        self._connection_factory: ConnectionFactory | None = None

    def set_connection_factory(self, factory: ConnectionFactory | None) -> None:
        """Test hook. Set to None to fall back to the real mcp SDK."""
        self._connection_factory = factory

    async def start(self, mcp_configs: dict[str, Any]) -> None:
        """Connect to every configured server. Failures are recorded per-server
        and do NOT abort startup — the affected tools just return errors when
        called."""
        for name, cfg in mcp_configs.items():
            try:
                connection, stack, description, raw_tools = await self._open_connection(cfg)
                tools, mapping = _translate_tools(name, raw_tools)
                self._servers[name] = MCPServer(
                    name=name,
                    description=description,
                    tools=tools,
                    raw_tool_names=mapping,
                    connection=connection,
                    exit_stack=stack,
                )
            except BaseException as exc:  # noqa: BLE001
                msg = _summarize_exception(exc)
                print(f"[mcp] failed to start server {name!r}: {msg}", file=sys.stderr)
                self._servers[name] = MCPServer(name=name, error=msg)

    async def stop(self) -> None:
        for srv in self._servers.values():
            if srv.exit_stack is not None:
                try:
                    await srv.exit_stack.aclose()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[mcp] error closing {srv.name!r}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
        self._servers.clear()

    def get(self, name: str) -> MCPServer | None:
        return self._servers.get(name)

    def all_servers(self) -> list[MCPServer]:
        return list(self._servers.values())

    def manifest(self) -> list[tuple[str, str, bool]]:
        """(server_name, description, is_ready) for every configured server —
        including failed ones so operators/agents can see what's broken."""
        return [(s.name, s.description, s.error is None) for s in self._servers.values()]

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> str:
        """Dispatch a call to a specific server's tool. `tool_name` is the
        raw name (already de-namespaced by the caller)."""
        srv = self._servers.get(server_name)
        if srv is None:
            raise MCPError(f"unknown MCP server: {server_name}")
        if srv.error is not None or srv.connection is None:
            raise MCPError(f"MCP server {server_name!r} unavailable: {srv.error}")
        try:
            result = await asyncio.wait_for(
                srv.connection.call_tool(tool_name, args),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MCPError(
                f"MCP call {server_name}.{tool_name} timed out after {timeout}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise MCPError(
                f"MCP call {server_name}.{tool_name} failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _extract_text(result)

    async def _open_connection(
        self, cfg: Any
    ) -> tuple[Any, AsyncExitStack, str, list[Any]]:
        """Return (connection, exit_stack, description, raw_tools).

        Uses the injected connection factory if set (tests), otherwise the
        real mcp SDK.
        """
        if self._connection_factory is not None:
            return await self._connection_factory(cfg)
        return await _real_open_connection(cfg)


# ---------------------------------------------------------------------------
# Real SDK bindings (lazy import — the manager is usable without them)
# ---------------------------------------------------------------------------


async def _real_open_connection(cfg: Any) -> tuple[Any, AsyncExitStack, str, list[Any]]:
    """Open a persistent MCP client session using the official Python SDK.

    Returns a tuple where `connection` exposes `call_tool(name, args)`. We
    thin-wrap ClientSession so the manager doesn't need to know about
    mcp.types-shaped return values.
    """
    from mcp import ClientSession, StdioServerParameters  # type: ignore[import-not-found]

    stack = AsyncExitStack()
    try:
        if cfg.transport == "stdio":
            from mcp.client.stdio import stdio_client  # type: ignore[import-not-found]

            params = StdioServerParameters(
                command=cfg.command, args=list(cfg.args), env=dict(cfg.env) or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif cfg.transport == "http":
            from mcp.client.streamable_http import (  # type: ignore[import-not-found]
                create_mcp_http_client,
                streamable_http_client,
            )

            # Use the SDK's helper so we inherit the MCP-safe defaults:
            # follow_redirects=True and a long SSE read timeout (~300s).
            # A bare httpx.AsyncClient(headers=...) inherits httpx's default
            # ~5s read timeout, which kills the initialize handshake before
            # the streaming response finishes.
            http_client = create_mcp_http_client(
                headers=dict(cfg.headers) if cfg.headers else None
            )
            await stack.enter_async_context(http_client)
            transport = await stack.enter_async_context(
                streamable_http_client(cfg.url, http_client=http_client)
            )
            read, write = transport[0], transport[1]
        else:
            raise MCPError(f"unknown transport: {cfg.transport}")

        session: ClientSession = await stack.enter_async_context(
            ClientSession(read, write)
        )
        init_result = await asyncio.wait_for(
            session.initialize(), timeout=cfg.timeout_seconds
        )
        description = ""
        server_info = getattr(init_result, "serverInfo", None)
        if server_info is not None:
            name = getattr(server_info, "name", "") or ""
            version = getattr(server_info, "version", "") or ""
            description = f"{name} {version}".strip()

        tools_result = await asyncio.wait_for(
            session.list_tools(), timeout=cfg.timeout_seconds
        )
        raw_tools = list(getattr(tools_result, "tools", []))

        return _SDKConnection(session), stack, description, raw_tools
    except BaseException:
        # If anything failed during setup, tear down whatever we opened.
        await stack.aclose()
        raise


class _SDKConnection:
    """Adapter over mcp.ClientSession. Exposes just `call_tool(name, args)`
    so the manager can stay SDK-agnostic."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return await self._session.call_tool(name, args)


# ---------------------------------------------------------------------------
# Schema translation + result flattening
# ---------------------------------------------------------------------------


def _translate_tools(
    server_name: str, raw_tools: list[Any]
) -> tuple[list[ToolSchema], dict[str, str]]:
    schemas: list[ToolSchema] = []
    mapping: dict[str, str] = {}
    for t in raw_tools:
        # `t` shape works for both mcp.types.Tool and plain dicts (tests).
        raw_name = _attr(t, "name")
        if not raw_name:
            continue
        description = _attr(t, "description") or ""
        input_schema = _attr(t, "inputSchema") or _attr(t, "input_schema") or {
            "type": "object",
            "properties": {},
        }
        namespaced = f"{server_name}{NAMESPACE_SEP}{raw_name}"
        schemas.append(
            ToolSchema(
                name=namespaced,
                description=description,
                input_schema=dict(input_schema),
            )
        )
        mapping[namespaced] = raw_name
    return schemas, mapping


def _extract_text(result: Any) -> str:
    """MCP call responses have a `content` list of TextContent/ImageContent/…
    For v1, join all TextContent items with newlines. Non-text is skipped
    with a placeholder so the agent knows something was omitted.

    If `isError` is set, we prefix the payload to make it obvious.
    """
    if isinstance(result, str):
        return result
    is_error = _attr(result, "isError", False)
    content = _attr(result, "content", None)
    parts: list[str] = []
    if content is None:
        text = _attr(result, "text", None)
        if isinstance(text, str):
            parts.append(text)
    else:
        for item in content:
            item_type = _attr(item, "type", None)
            if item_type == "text":
                parts.append(str(_attr(item, "text", "")))
            elif item_type == "image":
                parts.append("(image content omitted)")
            elif item_type == "resource":
                uri = _attr(_attr(item, "resource", {}) or {}, "uri", "?")
                parts.append(f"(resource: {uri})")
            elif isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
    body = "\n".join(p for p in parts if p) or "(empty response)"
    if is_error:
        return f"MCP tool error: {body}"
    return body


def _summarize_exception(exc: BaseException) -> str:
    """Unwrap ExceptionGroup/BaseExceptionGroup so the real cause surfaces.
    Without this, `except Exception` catches the group and .args says nothing
    useful — actionable details live in .exceptions."""
    if hasattr(exc, "exceptions"):
        subs = getattr(exc, "exceptions", None) or []
        if subs:
            inner = "; ".join(_summarize_exception(e) for e in subs)
            return f"{type(exc).__name__}({inner})"
    return f"{type(exc).__name__}: {exc}"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Safe access that handles both objects and dicts uniformly."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_manager: MCPManager | None = None


def get_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


def reset_for_tests() -> None:
    """Wipe the singleton. Called by pytest fixtures between tests."""
    global _manager
    _manager = None
