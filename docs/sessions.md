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

## WebSocket session interaction

```
WS /agents/{name}/sessions/{sid}
Authorization: Bearer <auth_secret>           # header, or ?token=... in URL
```

The WS protocol is described in [design/design.md §12.2](../design/design.md).
Quick summary of the event shapes the client sees:

| Server → client | When |
|---|---|
| `{ "type": "assistant_delta", "text": ... }` | Streaming assistant text |
| `{ "type": "assistant_message", "text": ... }` | End of one provider turn |
| `{ "type": "thinking", "delta": ... }` | Extended-thinking text (Gemini/Anthropic) |
| `{ "type": "tool_call", "id", "name", "input" }` | Model invoked a tool |
| `{ "type": "tool_result", "id", "output", "error" }` | Tool returned |
| `{ "type": "file_available", "path", "description", "size" }` | Agent shared a file (see [files.md](files.md)) |
| `{ "type": "injected_message", "from_session_id", "text" }` | Another session injected a message |
| `{ "type": "error", "message" }` | Server error during the turn |
| `{ "type": "done", "stop_reason" }` | Whole run-loop finished, awaiting next user input |

| Client → server | Effect |
|---|---|
| `{ "type": "user_message", "text": ... }` | Start a new turn |
| `{ "type": "stop" }` | Request cancellation (v1: no-op — see design.md §6) |

Per-session context is **not** added over the WS — it's a REST operation
even mid-chat. The CLI does the REST call when you type `/context ...`.

## Cross-session messaging

An agent can inject a message into another of its own sessions via the
built-in `post_to_session` tool. The receiving session records the message
in history and pushes a `file_available`-style `injected_message` event to
any connected WS clients. See [design/design.md §8](../design/design.md) for
the rationale; the implementation lives in [ark/broker.py](../ark/broker.py)
and the `post_to_session` tool in [ark/tools.py](../ark/tools.py).

## Heartbeat and cron sessions

When a heartbeat fires or a cron expression matches, the scheduler creates
a fresh session of kind `heartbeat` or `cron` and runs the same turn loop a
conversational session uses. The starting prompt comes from
`<ARK_HOME>/agents/<name>/heartbeat_prompt.md` (heartbeats) or the cron
entry's `prompt` column (crons). Scheduled sessions can post to
conversational sessions via `post_to_session` to surface results to humans.

Adding per-session context to a scheduled session is unusual but works —
the same REST endpoint accepts any session id regardless of kind.
