# Projects

A **project** is a shared, user-visible working directory that one or more
sessions can be bound to. Unlike an agent's workspace (per-agent, private),
a project is intended for files the user inspects, edits, uploads to, and
watches changing in real time.

Typical uses:

- A document the agent is drafting (the agent writes, the user edits, both
  iterate).
- A code repository the agent is reasoning over.
- A shared scratch space for a long-running task where the final artifacts
  matter more than the chat transcript.

Projects are persistent and survive across sessions, restarts, and (softly)
deletion. The agent's workspace is unchanged — when a session is in a
project, the workspace still exists as private scratch space; the project
is just the default working surface.

## Lifecycle

```
POST   /projects                  # create
GET    /projects                  # list active; ?include_deleted=true for all
GET    /projects/{id}             # fetch one
PUT    /projects/{id}             # update name/description/project_context
DELETE /projects/{id}             # soft-delete (files survive)
```

### Create

```json
POST /projects
{
  "name": "marketing-brochure",
  "root": "/var/lib/ark/projects/abc-123",    // optional; default below
  "description": "Q4 product brochure draft",  // optional
  "project_context": "Tone: warm, professional.\nAudience: enterprise."   // optional
}
```

Defaults:

- If `root` is omitted, the directory is created at
  `<ARK_HOME>/projects/<id>/`.
- If `root` points at an existing directory, the project adopts it as-is.
- If `root` points at a path that doesn't exist, the directory is created.

Names must be unique among active projects. A soft-deleted project's name
becomes available again.

### Soft-delete

`DELETE /projects/{id}` flips `deleted_at` on the row. **Nothing on disk is
touched.** Sessions that were bound to the project retain their
`project_id` column for history continuity, but `runtime.session_project()`
returns `None` for them so the runtime stops layering project context.

To "really" delete a project, do the row deletion via SQL plus `rm -rf` on
the directory. There is intentionally no API for that.

## Per-project filesystem API

Each project has a filesystem rooted at its `root`. Clients can browse,
read, write, and delete files through these endpoints:

```
GET    /projects/{id}/files                  # list root contents
GET    /projects/{id}/files/{path}           # file: stream bytes; dir: list JSON
PUT    /projects/{id}/files/{path}           # write file (raw body)
DELETE /projects/{id}/files/{path}           # delete file or directory (recursive)
POST   /projects/{id}/files/{path}?op=mkdir
POST   /projects/{id}/files/{path}?op=rename&dest=<dest>   # move/rename
```

`op=rename` behavior:

- `path` is the source, `dest` is the new location — both project-relative.
- Works on files and directories (moving a directory carries its subtree).
- Parent directories of `dest` are created if missing.
- `404` if the source doesn't exist; `409` if `dest` already exists (no
  silent overwrite).
- `dest` is path-traversal checked the same way `path` is — any attempt to
  rename out of the project root returns `400`.

Listings return:

```json
{
  "path": "subdir",
  "entries": [
    {"name": "draft.md", "is_dir": false, "size": 1234, "mtime": 1779000000000},
    {"name": "images",   "is_dir": true,  "size": 0,    "mtime": 1779000000000}
  ]
}
```

Path traversal is enforced — `..`, absolute paths, encoded variants, and
symlinks that escape the root all return `400`.

## Binding a session to a project

Pass `project_id` when creating the session:

```json
POST /agents/{name}/sessions
{
  "project_id": "abc-123",         // optional; null = workspace-only session
  "context": "..."                  // existing per-session context option
}
```

The binding is **immutable for the life of the session** — to operate in a
different project, start a new session.

When a session is in a project, the runtime adjusts three things:

1. **System prompt** gains a Project section between the Environment stanza
   and any per-session context:

   ```
   Project (this session):
   - Name: marketing-brochure
   - Root: /var/lib/ark/projects/abc-123
   - Description: Q4 product brochure draft

   Tone: warm, professional.
   Audience: enterprise.

   All file operations should target paths under the project root above
   unless explicitly asked to modify your workspace. The project is where
   the user can see and edit your work; your workspace is private scratch
   space. Uploads in this session land in `<project_root>/uploads/`.
   ```

2. **Uploads** (`POST /agents/{name}/sessions/{sid}/uploads`) land in
   `<project_root>/uploads/` instead of the workspace. `list_uploads` (REST
   and agent tool) lists the same directory.

3. **`cwd` is unchanged**. The agent's working directory is still its
   workspace — by design, for predictability. The agent uses absolute paths
   for project file ops, and that's what the system prompt directs it to
   do.

## Agent tools

| Tool | Behavior |
|---|---|
| `get_current_session_info()` | Now includes `project_id`, `project_name`, `project_root` (null when the session isn't in a project). |
| `get_project_info()` | Returns the project record (id, name, root, description, project_context) or null. Use when you want richer project metadata. |
| `list_uploads()` | Lists `<project_root>/uploads/` when in a project, `<workspace>/uploads/` otherwise. |
| `read_file` / `write_file` / `list_files` / `run_command` | Unchanged. The agent uses absolute paths to operate on project files. |

## Live file-change events

When a client is connected to the unified `/events` WebSocket, it also
receives:

```json
{
  "type": "project_file_changed",
  "project_id": "abc-123",
  "path": "subdir/draft.md",
  "change": "created" | "modified" | "deleted"
}
```

These come from a `watchdog`-based per-project filesystem watcher. Events
are coalesced within a ~200ms window to collapse editor-save bursts. The
following paths are ignored by default to keep noise down: `.git`,
`node_modules`, `__pycache__`, `.pytest_cache`, `.venv`, `.DS_Store`,
`.idea`, `.vscode`.

Move events surface as a delete on the source path followed by a create on
the destination.

## Multiple agents in one project

Projects are not coupled to agents. You can bind sessions from `scribe`,
`editor`, and `reviewer` all to the same project, and they'll all see the
same root, system-prompt section, and upload location. This is intended —
projects represent shared artifacts, not agent-private territory.

## What v1 doesn't cover

- **Heartbeat and cron sessions in projects.** The scheduler creates these
  without a `project_id` today. If you want a cron to operate inside a
  project, the cleanest workaround is to have the cron prompt include the
  project root path explicitly. A future revision could store `project_id`
  on cron entries.
- **Concurrent-edit locking.** If the agent is writing a file via
  `write_file` while the client is editing it via `PUT /files/...`, last
  write wins. No conflict detection in v1.
- **Per-project event filtering.** All connected `/events` clients hear
  about all project changes. Add client-side filtering on `project_id` if
  you only want a subset. Server-side per-project subscription is a
  possible future addition.
- **Restricted project roots.** A project root can point anywhere on the
  server filesystem. The Ark server runs as root in the intended
  deployment, so this is expected. Be deliberate.
