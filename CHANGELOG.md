# Changelog

## Unreleased — Workspace filesystem REST + live events

Adds a browsable / editable REST surface for an agent's workspace,
mirroring the project filesystem endpoints. The previously download-only
`GET /agents/{name}/files/{path}` now also returns directory listings when
the target is a directory, and is joined by `PUT` / `DELETE` / `POST ?op=mkdir`
for symmetry with `/projects/{id}/files/...`. Every agent's workspace is
now also filesystem-watched, with changes fanning out as a new
`workspace_file_changed` event on `/events`.

### New on the wire

**REST**:

```
GET    /agents/{name}/files                  # list workspace root
GET    /agents/{name}/files/{path}           # file → bytes; dir → JSON listing
PUT    /agents/{name}/files/{path}           # write file (raw body)
DELETE /agents/{name}/files/{path}           # delete file or empty dir
POST   /agents/{name}/files/{path}?op=mkdir
```

**New WS event** on `/events`:

```json
{
  "type": "workspace_file_changed",
  "agent_name": "scribe",
  "path": "scratch/draft.md",
  "change": "created" | "modified" | "deleted"
}
```

Same coalescing window and ignore-list as `project_file_changed`.

### Behavior change worth flagging

The existing `GET /agents/{name}/files/{path}` endpoint **adds a new
behavior** when the target path is a directory: it now returns a JSON
listing instead of 404. Files continue to stream as bytes. If a client was
relying on directory-paths returning 404, switch to checking the response
content-type or shape.

### Server-side internals

- `ark/file_watcher.py` generalized: a `FileWatcher` now hosts multiple
  *subjects* (kinds: `project` or `workspace`) with one Observer.
  `watch(kind, id, root)` / `unwatch(kind, id)`. Event-type and id-field
  mapping is data-driven.
- `ark/server.py`: workspace endpoints added; lifespan now starts a
  workspace watch for every configured agent at boot.
- No DB schema change.

## Unreleased — Projects

Adds a new concept of **projects** — shared user-visible working
directories that one or more sessions can be bound to. Unlike an agent's
private workspace, a project's contents are intended for the user to
inspect, edit, upload to, and watch changing in real time. Multiple agents
can work in one project.

See [docs/projects.md](docs/projects.md) for the full reference.

### New on the wire

**REST: project CRUD**

```
POST   /projects                         # create
GET    /projects                         # list active; ?include_deleted=true for all
GET    /projects/{id}
PUT    /projects/{id}                    # update name/description/project_context
DELETE /projects/{id}                    # soft-delete (files survive)
```

**REST: per-project filesystem**

```
GET    /projects/{id}/files
GET    /projects/{id}/files/{path}       # file → bytes; dir → JSON listing
PUT    /projects/{id}/files/{path}       # raw body
DELETE /projects/{id}/files/{path}
POST   /projects/{id}/files/{path}?op=mkdir
```

**Session creation** (`POST /agents/{name}/sessions`) now accepts an optional
`project_id` to bind the session to a project at creation. Binding is
immutable for the life of the session.

```diff
  POST /agents/{name}/sessions
  {
    "context": "...",
+   "project_id": "<project-uuid>"
  }
```

**New WS event** on `/events`:

```json
{
  "type": "project_file_changed",
  "project_id": "<uuid>",
  "path": "subdir/draft.md",
  "change": "created" | "modified" | "deleted"
}
```

Coalesced within ~200ms, with a default ignore-list (`.git`,
`node_modules`, `__pycache__`, etc.).

### New / changed agent tools

| Tool | Change |
|---|---|
| `get_current_session_info` | Now includes `project_id`, `project_name`, `project_root` (null when the session isn't in a project). |
| `get_project_info` | **NEW.** Returns the project record (id, name, root, description, project_context) or null. |
| `list_uploads` | Now dispatches: lists `<project_root>/uploads/` when in a project, `<workspace>/uploads/` otherwise. No call-site change. |

### Behavior changes for project sessions

- **System prompt** gains a "Project (this session)" section between the
  Environment stanza and per-session context. The agent is told the project
  root path and instructed to default to operating under it unless
  explicitly asked to modify the workspace.
- **Uploads** land in `<project_root>/uploads/` instead of the workspace's
  uploads dir.
- **`cwd` is unchanged** — still the agent's workspace. The agent uses
  absolute paths when operating on project files. This was a deliberate
  call to keep `cwd` predictable across all sessions.

### DB schema

Migration to user_version 3 adds:

- New `projects` table (id, name, root, description, project_context,
  created_at, deleted_at).
- New `project_id` column on `sessions` (nullable, FK).
- Unique index on `projects(name)` filtered to non-deleted rows, so a
  deleted project's name can be reused.

Applied automatically on server start.

### Client migration notes

This release is **additive** — existing clients that don't use projects
continue to work unchanged. To adopt projects:

1. Surface a project picker. On startup, `GET /projects` for the list.
2. When creating a session, optionally include `project_id` in the body.
3. Handle the new WS event type `project_file_changed`. Filter by
   `project_id` if you only care about specific projects.
4. Build a file browser / editor against the per-project filesystem
   endpoints. Listings are JSON; file contents are raw bytes.

### Server-side internals

- New `ark/projects.py` for CRUD + path resolution (mirrors `ark/workspace.py`).
- New `ark/file_watcher.py` — `watchdog`-based per-project filesystem
  watcher, with coalescing + ignore-list, publishing to the broker.
- `ark/runtime.py`: `session_project()` helper; `system_prompt` accepts a
  `project` arg.
- `ark/server.py`: project CRUD + filesystem endpoints; lifespan starts the
  watcher and adds watches for all active projects.
- `watchdog>=4.0` added to `requirements.txt`.

## Earlier — Unified event stream

(Original entry — see git history for details.) Replaced the per-session
WebSocket with a single per-client event stream (`WS /events`), added a
cross-session catch-up REST endpoint (`GET /events?since_id=...`), and
removed the old per-session WS endpoint. Every server-pushed event now
carries `session_id` and `agent_name`; commands sent over the WS specify
their target session in the body.
