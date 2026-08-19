# Sessions

A *session* is one conversation thread with an agent. Three kinds exist
(`conversational`, `heartbeat`, `cron` — see [design/design.md](../design/design.md)),
all sharing the same shape: a UUID, an agent owner, an ordered list of
messages, and an open-ended lifetime.

This doc covers the session-level API surface. For file transfer (uploads,
downloads, agent-shared artifacts) see [files.md](files.md).

## REST endpoints

All require `Authorization: Bearer <auth_secret>`.

### Create a session

```
POST /agents/{name}/sessions
Body (optional): { "context": "..." }
→ { "id": "<uuid>" }
```

Always creates a `conversational` session. If `context` is provided, it's
appended as the session's first `SessionContext` message — see
[Per-session context](#per-session-context) below.

Empty or absent bodies are accepted (creates an empty session, no context).
`Content-Type` of `application/json` is not strictly required for the empty
case.

### List sessions

```
GET /agents/{name}/sessions
GET /agents/{name}/sessions?kind=conversational&limit=20
→ [ { id, agent_name, kind, created_at, ended_at }, ... ]
```

Most recent first. `kind` filters to `conversational`, `heartbeat`, or
`cron`. `limit` defaults to 50.

### Read history

```
GET /agents/{name}/sessions/{sid}/history
→ [ { "kind": "<MessageKind>", "data": {...} }, ... ]
```

Returns every message in the session, ordered chronologically. `kind` is the
class name of the message (`UserText`, `AssistantText`, `ToolCall`,
`ToolResult`, `UploadMessage`, `SharedFile`, `SessionContext`).

### Delete a session

```
DELETE /agents/{name}/sessions/{sid}
→ { "ok": true }
```

Deletes the session row and (via cascade) all of its messages. Workspace
files survive — they're agent-scoped, not session-scoped. See
[files.md](files.md) for the file lifecycle nuance.

## Per-session context

The agent's persona and behavior come from `<ARK_HOME>/agents/<name>/session_context.md`
(see [config.md](config.md)) — that's set by whoever runs the server and is
the same across all sessions of one agent. **Per-session context** lets a
*client* layer additional instructions on top, just for one session, without
touching the agent file.

### How it stacks

The system prompt sent to the model is composed top-to-bottom:

```
<agent's session_context.md content>          ← agent persona/identity

---
Environment (managed by the Ark harness, do not invent paths):
- Your name: ...
- Your workspace directory: ...
- ...                                          ← runtime facts

---
Session context (provided by the client for this session —
additive, do not override the agent context above):
<context message 1>

<context message 2>                            ← client-supplied,
                                                  only if any have been added
```

The "do not override" line makes the layering legible to the model.

### Adding context

```
POST /agents/{name}/sessions
Body: { "context": "..." }                    ← seeds on session creation

POST /agents/{name}/sessions/{sid}/context
Body: { "context": "..." }                    ← appends mid-session
→ { "ok": true, "count": N }                  ← N = total context messages
```

The mid-session endpoint:
- **Always appends.** No replace, no edit. Multiple posts accumulate in
  the order they arrive.
- Rejects empty/whitespace-only text with `400`.
- Returns the total context-message count so the client can confirm.

### Behavior

- **Visibility timing.** New context shows up on the *next* user turn, not
  within an in-flight one. The system prompt is built once at the start of
  each turn; mid-tool-loop additions wait their turn.
- **Not sent as LLM messages.** `SessionContext` rows live in the session
  history (for audit + replay) but the runtime strips them before passing
  the message list to the provider. They contribute to the system prompt
  only — otherwise the model would see the same text twice.
- **History inspection.** They appear as `{"kind": "SessionContext"}` in
  `GET /history` so clients can see what's been added.
- **No DELETE.** Wipe-and-restart isn't supported in v1. If you really need
  a clean slate, delete the session and create a new one.

### CLI

```
ark chat <agent> --context "..."             # seed at creation
ark chat <agent> --context-file PATH         # read from file
ark chat <agent> --session SID --context "..." # append to a resumed session

# mid-chat
you> /context <additional instructions>
```

## The event stream (unified per-client)

Ark exposes a **single WebSocket per client** that carries events for every
session the bearer token has access to. The same connection delivers
streaming text from the active chat, cron-injected messages from other
sessions, tool calls happening in background runs, and so on — clients
multiplex on `session_id` to decide what to render where.

```
WS /events
Authorization: Bearer <auth_secret>           # header, or ?token=... in URL
```

Every event the server pushes has `session_id` and (where applicable)
`agent_name`. Commands the client sends carry `session_id` to route to the
right session.

### Server → client events

| Event | Fields | When |
|---|---|---|
| `assistant_delta` | `text` | Streaming assistant text |
| `assistant_message` | `text` | End of one provider turn |
| `thinking` | `delta` | Extended-thinking text (Gemini/Anthropic) |
| `tool_call` | `id`, `name`, `input` | Model invoked a tool |
| `tool_result` | `id`, `output`, `error` | Tool returned |
| `turn_usage` | `input_tokens`, `output_tokens`, `model`, `context_window` | Per-turn token counts (`context_window` is null if unknown). See [Usage tracking](#usage-tracking) below. |
| `file_available` | `path`, `description`, `size` | Agent shared a file (see [files.md](files.md)) |
| `injected_message` | `from_session_id`, `text` | Another session injected a message via `post_to_session` |
| `error` | `code`, `message` | Classified failure. `code` is one of `context_too_long`, `rate_limit`, `auth`, `other`. The runtime persists the same error as a `RunError` message in history. |
| `done` | `stop_reason` | Whole run-loop finished for that session, awaiting next user input. On classified errors, `stop_reason` is `"error:<code>"`. |

Every event also carries `session_id` and (except for the broad "error" case
where the session couldn't be identified) `agent_name`.

### Client → server commands

| Command | Required fields | Effect |
|---|---|---|
| `user_message` | `session_id`, `text` | Start a new turn in that session. Multiple sessions can have turns running concurrently — events stream back tagged with their `session_id`. |
| `stop` | `session_id` | Request cancellation (v1: no-op — see design.md §6). |

Per-session context is **not** added over the WS — it's a REST operation
even mid-chat. The CLI does the REST call when you type `/context ...`.

### Cross-session catch-up

```
GET /events?since_id=<int>&since_ms=<int>&limit=<int>
```

Returns persisted messages across *every* session, ordered by the monotonic
message id. Use this to fill the gap between disconnect and reconnect, to
compute unread counts, or to populate "what's new since I last opened the
app" UIs.

Response:

```json
{
  "events": [
    {
      "id": 12345,
      "session_id": "...",
      "agent_name": "scribe",
      "created_at": 1747852800000,
      "kind": "AssistantText",
      "data": { "text": "..." }
    },
    {
      "id": 12346,
      "session_id": "different-session",
      "agent_name": "vanto",
      "created_at": 1747852805000,
      "kind": "InjectedMessage",
      "data": { "text": "...", "from_session_id": "..." }
    }
  ],
  "next_since_id": 12346,
  "has_more": false
}
```

- `since_id` is the durable cursor — pass back what came as `next_since_id`
  on your previous call to resume cleanly.
- `since_ms` is a wall-clock-relative window (Unix milliseconds). Best-effort
  — wall-clock ties can be ambiguous; prefer `since_id` for "exact resume."
- Default (no cursor) returns the most recent `limit` events, ascending.
- `limit` defaults to 200, max 1000.
- Same `kind` translation as `/history` — including `InjectedMessage`
  surfacing for cross-session injections.

## Usage tracking

Every provider call (every iteration of the model→tools→model loop within
a user turn) emits a `turn_usage` event with token counts pulled from the
provider's response metadata. Two consumers:

- **Live UI**. The CLI prints a dim indicator after each turn:
  `[12,440/200,000 ctx (6.2%) · out 348 · claude-sonnet-4-6]`. When the
  model's context ceiling is unknown, the percentage is omitted.
- **Persistent record**. Each event is also written to the session's
  message log as a `TurnMetrics` row. These rows are filtered out before
  the message list is sent to the next provider call (they're telemetry,
  not conversation), but `GET /history` returns them so clients can sum
  token usage across a session.

**Context-ceiling source of truth.** The harness ships a small table of
known model → max input tokens in [ark/models.py](../ark/models.py).
You can override per-agent in config:

```json
"agents": {
  "scribe": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "max_context_tokens": 1000000   // opt into Anthropic's 1M beta
  }
}
```

If neither the table nor a config override has a value, `context_window`
is `null` in the event and the CLI shows raw counts only.

**No compaction in v1.** When a session's accumulated input would exceed
the model's window, the provider returns an error, the runtime classifies
it as `context_too_long` and records it as a `RunError` in history. The
CLI surfaces this with an actionable message. Recovery is "start a new
session." Compaction / summarization is a separate piece of work.

## Error tracking

Provider exceptions are caught inside `run_user_turn`, classified into one
of four codes, persisted as a `RunError` message, and surfaced over the WS
as an `error` event:

| Code | Triggered by |
|---|---|
| `context_too_long` | "context length exceeded" / "prompt is too long" / "input is too long" from any provider |
| `rate_limit` | 429s, "rate limit" in the message, or a `RateLimit*` exception type |
| `auth` | 401s, "authentication" / "invalid api key" in the message, or `Authentication*` exception types |
| `other` | Anything else |

After an error, the run loop ends with `stop_reason: "error:<code>"`. The
session is not deleted — history is fully readable and you can attempt
another turn (which will likely hit the same problem until you act on the
code).

## Cross-session messaging

An agent can inject a message into another of its own sessions via the
built-in `post_to_session` tool. The receiving session records the message
in history and pushes a `file_available`-style `injected_message` event to
any connected WS clients. See [design/design.md §8](../design/design.md) for
the rationale; the implementation lives in [ark/broker.py](../ark/broker.py)
and the `post_to_session` tool in [ark/tools.py](../ark/tools.py).

## Advisory model/effort overrides

Two session-create metadata keys get runtime meaning (everything else in
`metadata` stays opaque, surfaced to skills only):

```json
{ "metadata": { "model": "moonshotai/kimi-k3", "effort": "high" } }
```

- `model` — any provider model id; replaces the agent's configured model
  for this session's turns. The **effective** model is echoed in
  `turn_usage` events and persisted metrics, so clients always see what
  actually ran. A bad id fails the provider call and surfaces as the usual
  run error.
- `effort` — `medium` (the baseline, explicit no-op) or `high` (output
  budget raised to 8192 tokens, tool-loop ceiling to 32 iterations).
  Presets only ever *raise* the agent's configured budgets.

Absent or unrecognized values fall through to the agent's configuration —
fully backward compatible; clients that send nothing are unaffected.

## Session metadata + cron fire history

For debugging "what did the cron actually do," two endpoints + two CLI
commands:

```
GET /sessions/{sid}                                 # session metadata
GET /agents/{name}/crons/{cron_id}/sessions[?limit] # fires of a specific cron
```

The metadata endpoint returns `{id, agent_name, kind, created_at, ended_at,
project_id, cron_id, cron_prompt?}`. `cron_prompt` is present only when the
session is a cron fire — it's the prompt from the cron entry at the time the
fire was rendered, which makes transcripts self-explanatory.

The fire-history endpoint returns each fire enriched with a one-line
`summary` (the first `post_to_session` body, falling back to last
`AssistantText`, falling back to `"(no output)"`), plus `had_error` and
`error_code`. Clients can render a table without round-tripping `/history`
per row.

```bash
ark cron history <agent> <cron-id> [--limit N]
ark show <session-id>
```

`ark show` pretty-prints any session — cron, heartbeat, or conversational —
collapsing turns and surfacing `RunError` rows + token-usage metrics inline.

`sessions.cron_id` is populated only for sessions created by the scheduler
firing that cron. Historical sessions (pre-migration) keep a null
`cron_id` and won't surface in the new history endpoint.

## Heartbeat and cron sessions

When a heartbeat fires or a cron expression matches, the scheduler creates
a fresh session of kind `heartbeat` or `cron` and runs the same turn loop a
conversational session uses. The starting prompt comes from
`<ARK_HOME>/agents/<name>/heartbeat_prompt.md` (heartbeats) or the cron
entry's `prompt` column (crons). Scheduled sessions can post to
conversational sessions via `post_to_session` to surface results to humans.

Adding per-session context to a scheduled session is unusual but works —
the same REST endpoint accepts any session id regardless of kind.
