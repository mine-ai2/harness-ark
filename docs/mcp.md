# MCP servers

Ark speaks [Model Context Protocol](https://modelcontextprotocol.io) as a
client. Configured servers appear to agents as *skills* — bundles of related
tools discovered via `list_skills` and activated with `load_skill`, or made
always-available per agent. From the agent's perspective an MCP-backed tool
looks the same as any other tool, aside from its namespaced name.

## Configuration

Two config blocks, both optional and additive. Existing configs continue to
work unchanged.

### Top-level: which servers exist

```json
"mcp_servers": {
  "linear": {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-linear"],
    "env": { "LINEAR_API_KEY": "lin_..." }
  },
  "postgres": {
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-postgres", "--connection-string", "postgresql://..."],
    "timeout_seconds": 60
  },
  "notion": {
    "transport": "http",
    "url": "https://mcp.notion.com/mcp",
    "headers": { "Authorization": "Bearer nti_..." }
  }
}
```

| Field | Type | Applies to | Notes |
|---|---|---|---|
| `transport` | `"stdio"` or `"http"` | both | Required. `stdio` spawns a subprocess and speaks JSON-RPC over pipes; `http` opens a Streamable HTTP client. |
| `command` | string | stdio | Required for stdio. Executable to spawn (e.g. `npx`, `uvx`, `python`). |
| `args` | string[] | stdio | Arguments to pass to `command`. |
| `env` | object | stdio | Environment variables for the subprocess. Common home for API keys. |
| `url` | string | http | Required for http. The MCP endpoint URL. |
| `headers` | object | http | Additional headers to send on every request (e.g. `Authorization`). |
| `timeout_seconds` | number | both | Per-call deadline (also used for the initial `list_tools`). Default 30. |

### Per-agent: which servers this agent can use

```json
"agents": {
  "scribe": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "mcp_servers": ["linear", "notion"],
    "always_loaded_mcp_servers": ["linear"]
  }
}
```

`mcp_servers` gates which servers this agent can see. A server absent from
this list is completely invisible to the agent — it can't call
`load_skill("linear")` or invoke `linear__anything` even if the server is
running.

`always_loaded_mcp_servers` (must be a subset of `mcp_servers`) puts those
servers' tool schemas in the manifest on every turn, mirroring
`always_loaded_skills`. Everything else is lazy — the agent calls
`load_skill("linear")` to bring Linear's tools into scope for the rest of
the session.

## How agents see MCP tools

Tool names are namespaced with the server name using a double-underscore
separator (`linear__create_issue`, `postgres__query`). This is universal
across model providers — dots break some tool-schema validators.

`list_skills` shows both Python skills and MCP servers, tagged with `(mcp)`:

```
notes — take and organize personal notes
linear (mcp) [loaded] — Linear MCP server v1.2
postgres (mcp) — postgres 0.4
```

`load_skill("linear")` and `load_skill("notes")` both work — the
distinction between MCP-backed and Python-backed skills is invisible from
the agent's side (which is the point).

## Lifecycle

Connections are established once at server boot and reused across every
agent, session, and turn. On shutdown, all subprocesses and HTTP clients
close cleanly.

Per-server startup failures don't abort Ark — the server is marked
unavailable and its tools return errors when called. `GET /agents/{name}`
surfaces per-agent MCP status:

```json
{
  "mcp_servers": [
    { "name": "linear", "ready": true, "error": null, "tool_count": 12,
      "always_loaded": true },
    { "name": "postgres", "ready": false,
      "error": "FileNotFoundError: uvx", "tool_count": 0,
      "always_loaded": false }
  ]
}
```

## Failure semantics

| Case | Behavior |
|---|---|
| Server can't start at boot | Logged to stderr, marked unavailable. Tools return `ToolError` when called. Ark keeps running. |
| Tool call times out | After `timeout_seconds`, tool returns `ToolError("MCP call server.tool timed out after Ns")`. |
| Tool call raises upstream | Wrapped as `ToolError("MCP call server.tool failed: <type>: <msg>")`. |
| Server returns `isError: true` | Result is prefixed with `MCP tool error:` so the agent sees it explicitly. |
| Response contains non-text content | Text parts are joined; images become `(image content omitted)`; resources become `(resource: <uri>)`. Binary/rich content in MCP responses is not supported in v1. |

## Sharp edges worth knowing

1. **stdio servers are subprocesses of `uvicorn`**. Ark's lifespan closes them
   on shutdown, but if uvicorn is `SIGKILL`'d they can be left as zombies for
   whoever inherits the process group. `systemctl restart` handles this
   cleanly; ad-hoc `kill -9` doesn't.

2. **MCP tools can't see Ark internals**. There's no `current_context()` on
   the other side of the pipe. An MCP tool doesn't know which session or
   agent is calling it, and can't touch the DB or workspace. That's the
   right boundary — MCP is for *external* integrations.

3. **Token bloat if you always-load too much**. A single MCP server can
   expose 50+ tools. Multiple always-loaded servers × per-turn manifest =
   real cost. Prefer lazy loading (agent calls `load_skill` when it needs
   Linear) unless the agent uses those tools on almost every turn.

4. **Schema quirks per provider**. Some MCP tools declare JSON Schema
   features that Gemini rejects (nested `oneOf`, uncommon `format`
   values). If a turn fails with a validation error naming an MCP tool,
   that's the cause — the schema needs sanitizing on the MCP-server side.

5. **No per-tool ACL yet**. You can gate at the server level (whitelist
   which servers an agent may access) but can't say "scribe can call
   `linear__list_issues` but not `linear__delete_issue`." Comes later.
