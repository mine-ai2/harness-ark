# Ark — v1 Design

> "The Home of the Autobots on Earth"

## 1. Overview

Ark is a configurable server process that runs on a UNIX machine and hosts one or more LLM-powered agents. Each agent can be activated by:

- **Conversational sessions** — a client opens a session over a websocket and exchanges messages with the agent.
- **Timer events** — the agent is woken up in a fresh session with a starting prompt. Timers come in two flavors:
  - **Heartbeats** — fire every N seconds.
  - **Crons** — fire on a UNIX cron expression.

During any session the agent can push a message into another session that the **same agent** owns. The injected message is recorded as if the agent had said it. The receiving session does not see the message until its next turn, at which point it appears in the session history — this is how scheduled work surfaces back into a user-facing session.

Inspirations: OpenClaw (multi-agent + extensibility), Claude Code (tool-use model), NotebookLM (durable, contextual agents).

Out of scope for v1: multi-user auth, multi-agent communication, sandboxing, public network exposure, hot config reload.

## 2. Core Concepts

| Concept | Description |
|---|---|
| **Agent** | A named LLM persona with its own workspace, schedule, model config, and skill set. |
| **Session** | A single conversation thread. Kind is `conversational`, `heartbeat`, or `cron`. |
| **Workspace** | A directory on disk that the agent reads and writes. One workspace per agent by default. |
| **Injected message** | A message one session pushes into another session owned by the same agent. Stored as an assistant turn in the target session's history. |
| **Skill** | A Python module exposing `@tool`-decorated functions, loaded lazily by the agent to avoid token bloat. |
| **Tool** | Anything the LLM can invoke. Includes built-in tools (file/bash/web) and skill-defined tools once loaded. |

## 3. Filesystem Layout

```
~/.ark/
  config.json              # Ark configuration (see §4)
  ark.db                   # SQLite: sessions, messages, notifications, schedules
  agents/
    <agent-name>/
      session_context.md   # Provided at the start of every session for this agent
      heartbeat_prompt.md  # Used as the starting prompt for heartbeat sessions
      workspace/           # The agent's working directory (default location)
      skills/              # Agent-local skills (in addition to global skills)
  skills/                  # Global skills, available to every agent
```

The agent's `cwd` when invoking tools is its workspace directory. The agent may read any file in its workspace, and may write any file in its workspace except `session_context.md` and `heartbeat_prompt.md` (these are user-managed). Cron entries live only in the database (§13) and are edited through the `schedule` meta-tool or the CLI.

> Built-in file/bash tools are *not* sandboxed to the workspace — see §9. The workspace is just a convenient default `cwd` and the location where the agent's own outputs live.

## 4. Configuration

`~/.ark/config.json`:

```json
{
  "server": {
    "host": "127.0.0.1",
    "port": 7777,
    "auth_secret": "..."
  },
  "providers": {
    "anthropic": { "api_key": "..." },
    "openai": { "api_key": "..." }
  },
  "tools": {
    "brave_search": { "api_key": "..." }
  },
  "agents": {
    "scribe": {
      "provider": "anthropic",
      "model": "claude-opus-4-7",
      "endpoint": "https://api.anthropic.com/v1/messages",
      "workspace": "~/.ark/agents/scribe/workspace",
      "always_loaded_skills": ["notes", "calendar"]
    }
  }
}
```

- Config is read once at server start. Edits require a restart in v1.
- `auth_secret` is the single shared bearer token used by all clients (single-user v1).
- Per-agent `workspace` defaults to `~/.ark/agents/<name>/workspace` if omitted.
- `always_loaded_skills` lists skills whose tool schemas are exposed to the model on every session start (see §10).

## 5. Agents

An agent is defined entirely by its entry in `config.json` plus the contents of `~/.ark/agents/<name>/`. Adding an agent means: (1) add a config entry, (2) create the agent directory, (3) restart.

Each agent has:
- A **name** (unique, used in API paths and CLI).
- A **provider + model + endpoint** (see §11).
- A **workspace** directory.
- A **session_context.md** seeded at the start of every session as a system prompt.
- A **heartbeat schedule** (optional, in seconds) and any number of **cron entries**.
- A **skill manifest** (global skills + agent-local skills + always-loaded subset).

## 6. Sessions

Three kinds, all uniform in structure:

| Kind | Trigger | Initial prompt source |
|---|---|---|
| `conversational` | Client opens websocket | First user message |
| `heartbeat` | Timer fires | `heartbeat_prompt.md` |
| `cron` | Cron expression matches | `crons.prompt` column (§13) |

Each session has a stable UUID, an agent owner, a kind, and a creation timestamp. Sessions persist indefinitely; deletion is explicit.

**Concurrency**: multiple sessions for the same agent may run simultaneously, including overlapping heartbeats. Each runs in isolation; the only shared state is the filesystem workspace. Agents and skills must tolerate concurrent file access — Ark provides no locking.

**Session ending**: a session ends when (a) the model returns a final assistant turn with no tool calls and the websocket closes, or (b) the user/client issues `stop`, or (c) for scheduled sessions, when the model returns without further tool calls. Sessions are not auto-resumed; reopening a websocket on a closed session continues it.

**Cancellation (`stop`)**: interrupts the in-flight model call and kills any tool subprocess currently running for that session (SIGTERM, then SIGKILL after 5s).

## 7. Scheduling

The scheduler is an in-process loop that wakes once per second, checks all heartbeat counters and cron expressions, and starts new sessions as triggers fire.

- Schedules are stored in SQLite (`agent_state` for heartbeat interval; `crons` for cron entries).
- Heartbeat fires create a new session each time; if a prior heartbeat session is still running, a new one starts alongside it.
- Agents manage their own schedule through the `schedule` meta-tool (see §9). Changes take effect immediately.

## 8. Cross-Session Messaging

Sessions can talk to each other, but only within a single agent. This is how scheduled work (heartbeats, crons) gets in front of the user.

- Any session can call `post_to_session(session_id, body)` to inject a message into another session owned by the same agent. Cross-agent injection is rejected.
- The injected message is persisted as an `assistant`-role message in the target session's `messages` table, tagged with the source session id in `content_json` so it's debuggable and the agent can tell user-typed turns from scheduled injections.
- The agent currently *in* the target session does not see the injected message mid-turn. It lands in history; the next time that session runs (next user turn, next heartbeat continuation, etc.) the message is part of the conversation the model receives.
- If a client has a live websocket open on the target session at the moment of injection, Ark emits the same `assistant_message` event to that client so it appears immediately in the UI.

Typical flow: a `cron` session does its work, then calls `list_my_sessions(kind="conversational", limit=1)` to find the user's active session and `post_to_session(...)` to drop in a summary. Next time the user looks at that session, the summary is in history.

## 9. Built-in Tools

All built-in tools run with the privileges of the Ark server process. There is **no approval flow**; the LLM may choose to ask the user before destructive actions, but Ark does not gate calls.

| Tool | Description |
|---|---|
| `read_file(path)` | Read any file on the filesystem accessible to the server process. |
| `write_file(path, content)` | Write any file. Creates parent dirs. |
| `list_files(path, pattern?)` | List/glob files. |
| `run_command(cmd, timeout_seconds=60)` | Run a shell command. Default timeout 60s, max 600s. Killed on `stop`. |
| `search_web(query)` | Brave Search API. |
| `post_to_session(session_id, body)` | Inject a message into another session owned by the same agent. See §8. |
| `list_my_sessions(kind?, limit?)` | List sessions owned by the current agent, most recent first. |
| `list_skills()` | Meta-tool. Returns the skill manifest (name + one-line description). |
| `load_skill(name)` | Meta-tool. Exposes that skill's tool schemas for the rest of the session. |
| `schedule(op, ...)` | Meta-tool. Manage own heartbeat/crons: `set_heartbeat(seconds)`, `add_cron(id, expr, prompt)`, `remove_cron(id)`, `list_crons()`. |

## 10. Skills

Skills are how Ark gets OpenClaw-style extensibility without paying the token cost of exposing every possible tool on every turn.

### 10.1 Definition

A skill is a Python module placed in `~/.ark/skills/` (global) or `~/.ark/agents/<name>/skills/` (agent-local). The module's docstring is the skill's one-line description shown in the manifest.

```python
# ~/.ark/skills/notes.py
"""Capture and retrieve quick notes in a per-agent notebook."""

from ark.skills import tool

@tool
def add_note(title: str, body: str) -> str:
    """Append a note to the notebook.

    Args:
        title: Short headline for the note.
        body: Markdown content.
    Returns:
        Confirmation string with the note id.
    """
    ...

@tool
def search_notes(query: str, limit: int = 10) -> list[dict]:
    """Search notes by full-text match. Returns most recent first."""
    ...
```

The `@tool` decorator inspects type hints and the docstring to produce a provider-agnostic JSON schema (see §11). Skills run in-process (same Python interpreter as the server) with the agent's workspace as `cwd`.

### 10.2 Lazy loading

- On session start, the model sees:
  - All built-in tools (§9), including `list_skills` and `load_skill`.
  - Tool schemas for any **always-loaded** skills declared in the agent's config.
  - **No schemas** for the rest — just names + descriptions surfaced via `list_skills()`.
- When the model calls `load_skill("notes")`, Ark adds that skill's tool schemas to the tool list for the remainder of the session. Subsequent turns include those schemas.
- Loading is per-session and not persisted.

This means a deployment with 50 skills costs only ~50 lines of manifest text per session unless the agent reaches for one.

### 10.3 Versioning and reloading

Skills are imported at server start. v1 does not hot-reload skills; editing a skill requires a restart. (Punted: dynamic reload, skill dependencies, skill-level config.)

## 11. Provider Abstraction

v1 supports Anthropic and OpenAI behind a single internal interface:

```python
class Provider:
    def stream_turn(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> Iterator[StreamEvent]: ...
```

`StreamEvent` is a normalized union: `TextDelta`, `ToolCall`, `ToolResult`, `TurnEnd`, `Thinking` (Anthropic only; ignored events on OpenAI).

Per-provider adapters translate Ark's internal `ToolSchema` into the provider's tool format, and translate stream chunks back into `StreamEvent`. Tool inputs are normalized to JSON objects regardless of provider.

## 12. Server API

Base URL: `http://<host>:<port>`. All REST requests carry `Authorization: Bearer <auth_secret>`. Websocket connections carry the same header (or `?token=` for browser clients).

### 12.1 REST

| Method | Path | Description |
|---|---|---|
| GET  | `/agents` | List agents with summary status (running session count, heartbeat interval, cron count). |
| GET  | `/agents/:name` | Full detail: model, schedules, loaded skills, recent sessions. |
| PUT  | `/agents/:name/heartbeat` | Body `{"interval_seconds": N \| null}`. |
| GET  | `/agents/:name/crons` | List cron entries. |
| PUT  | `/agents/:name/crons/:id` | Upsert `{"expr": "...", "prompt": "..."}`. |
| DELETE | `/agents/:name/crons/:id` | Remove. |
| GET  | `/agents/:name/sessions` | List sessions (paginated, filter by kind). |
| POST | `/agents/:name/sessions` | Create a conversational session. Returns id. |
| DELETE | `/agents/:name/sessions/:id` | Delete a session and its history. |
| GET  | `/agents/:name/sessions/:id/history` | Full message log. |

### 12.2 Websockets

**`WS /agents/:name/sessions/:id`** — live session interaction.

Client → server:
```
{ "type": "user_message", "text": "..." }
{ "type": "stop" }
```

Server → client:
```
{ "type": "thinking", "delta": "..." }
{ "type": "assistant_delta", "text": "..." }
{ "type": "assistant_message", "text": "..." }       // turn complete
{ "type": "tool_call", "id": "...", "name": "...", "input": {...} }
{ "type": "tool_result", "id": "...", "output": "...", "error": null }
{ "type": "injected_message", "from_session_id": "...", "text": "..." }
{ "type": "error", "message": "..." }
{ "type": "done" }                                    // model turn ended, awaiting input
```

`injected_message` is emitted only when another session of the same agent calls `post_to_session` against this session while the client is connected. The message has already been persisted to history at this point.

## 13. Storage

Single SQLite database at `~/.ark/ark.db`.

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  agent_name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- 'conversational' | 'heartbeat' | 'cron'
  created_at INTEGER NOT NULL,
  ended_at INTEGER
);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,            -- 'system' | 'user' | 'assistant' | 'tool_call' | 'tool_result'
  content_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE agent_state (
  agent_name TEXT PRIMARY KEY,
  heartbeat_seconds INTEGER
);

CREATE TABLE crons (
  agent_name TEXT NOT NULL,
  id TEXT NOT NULL,
  expr TEXT NOT NULL,
  prompt TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (agent_name, id)
);
```

Cron entries live only in this table. They are created and edited through the `schedule` meta-tool (agent) or the CLI (user) — there is no on-disk representation.

## 14. CLI Client

A small CLI (`ark`) that talks to the local server:

```
ark agents                              # list
ark agent <name>                        # detail
ark chat <name>                         # opens a conversational session, streams over WS
ark chat <name> --session <id>          # resume an existing session
ark sessions <name>                     # list sessions (most recent first), --kind to filter
ark cron list <agent>
ark cron set <agent> <id> "<expr>" -p <prompt_file>
ark heartbeat set <agent> <seconds>
```

Config: `~/.ark/cli.json` with `{ "server": "http://127.0.0.1:7777", "auth_secret": "..." }`.

## 15. v1 Build Order (Suggested)

1. Config loader + SQLite migrations + agent directory bootstrapping.
2. Anthropic provider adapter + normalized stream events.
3. Built-in tools (file, bash, search).
4. Session runtime: turn loop, tool dispatch, message persistence.
5. REST API.
6. Websocket session interaction.
7. CLI (chat + sessions first).
8. Scheduler (heartbeats then crons).
9. Schedule meta-tool.
10. Cross-session messaging: `post_to_session` + `list_my_sessions` + `injected_message` WS event.
11. Skills loader + `list_skills` / `load_skill` meta-tools.
12. OpenAI provider adapter.

## 16. Open Questions / Deferred

- **Cost & token accounting** per session — capture in messages table but no UI in v1.
- **Audit log** of tool calls — covered by `messages` for now.
- **Skill state / context object** — v1 skills are plain functions with the workspace as `cwd`. If skills need session id, agent name, etc., we'll add a contextvar-based injection later.
- **Hot reload** of config and skills.
- **Sandboxing** of file/bash tools.
- **Multi-agent communication** — agents do not see each other's sessions or notifications in v1.
