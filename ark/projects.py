"""Projects — shared, user-visible working directories that sessions can be
bound to.

A project has a database row and a filesystem root. The row is the source of
truth; the directory is created on demand if it doesn't already exist. Names
must be unique among non-deleted projects.

Soft-delete only: `DELETE` flips `deleted_at` on the row; files on disk are
left intact. Sessions retain their `project_id` after deletion so history
keeps working.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from . import paths
from .types import Project


class ProjectError(Exception):
    pass


def projects_root_dir() -> Path:
    return paths.ark_home() / "projects"


def default_root_for(project_id: str) -> Path:
    return projects_root_dir() / project_id


def now_ms() -> int:
    return int(time.time() * 1000)


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    root: str | None = None,
    description: str = "",
    project_context: str = "",
) -> Project:
    name = (name or "").strip()
    if not name:
        raise ProjectError("name is required")
    pid = str(uuid.uuid4())
    root_path = Path(root).expanduser().resolve() if root else default_root_for(pid)
    if not root_path.is_absolute():
        raise ProjectError(f"root must be an absolute path: {root!r}")
    # Create the directory if it doesn't already exist. If it does, we adopt it
    # as-is — useful for pointing a project at an existing folder.
    root_path.mkdir(parents=True, exist_ok=True)
    try:
        conn.execute(
            "INSERT INTO projects(id, name, root, description, project_context, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (pid, name, str(root_path), description, project_context, now_ms()),
        )
    except sqlite3.IntegrityError as e:
        raise ProjectError(f"a project named {name!r} already exists") from e
    return _row_to_project(
        conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    )


def get(conn: sqlite3.Connection, project_id: str) -> Project | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_project(row)


def list_projects(
    conn: sqlite3.Connection, *, include_deleted: bool = False
) -> list[Project]:
    sql = "SELECT * FROM projects"
    if not include_deleted:
        sql += " WHERE deleted_at IS NULL"
    sql += " ORDER BY created_at DESC"
    return [_row_to_project(r) for r in conn.execute(sql).fetchall()]


def update(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    project_context: str | None = None,
) -> Project:
    p = get(conn, project_id)
    if p is None:
        raise ProjectError(f"unknown project {project_id!r}")
    if p.deleted_at is not None:
        raise ProjectError(f"project {project_id!r} has been deleted")
    fields: list[str] = []
    params: list = []
    if name is not None:
        name = name.strip()
        if not name:
            raise ProjectError("name cannot be empty")
        fields.append("name = ?")
        params.append(name)
    if description is not None:
        fields.append("description = ?")
        params.append(description)
    if project_context is not None:
        fields.append("project_context = ?")
        params.append(project_context)
    if not fields:
        return p
    params.append(project_id)
    try:
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", params)
    except sqlite3.IntegrityError as e:
        raise ProjectError(f"a project named {name!r} already exists") from e
    return _row_to_project(
        conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    )


def soft_delete(conn: sqlite3.Connection, project_id: str) -> bool:
    """Soft-delete a project. Returns True if it was active, False if missing
    or already deleted. Never touches files on disk."""

    row = conn.execute(
        "SELECT deleted_at FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None or row["deleted_at"] is not None:
        return False
    conn.execute(
        "UPDATE projects SET deleted_at = ? WHERE id = ?", (now_ms(), project_id)
    )
    return True


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


class ProjectPathError(Exception):
    pass


def resolve_path(project: Project, relative: str) -> Path:
    """Resolve a path within a project, guarding against traversal.

    Returns the absolute path. Raises ProjectPathError if the resolved path
    would land outside the project root (including via symlinks).
    """

    root_real = Path(project.root).resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ProjectPathError(f"path must be project-relative: {relative!r}")
    # Strip any leading "./" or "/" segments. relative path components only.
    full = (root_real / candidate).resolve()
    try:
        full.relative_to(root_real)
    except ValueError:
        raise ProjectPathError(f"path escapes project root: {relative!r}")
    return full


def relative_to_root(project: Project, full: Path) -> str:
    root_real = Path(project.root).resolve()
    return str(full.resolve().relative_to(root_real))


# ---------------------------------------------------------------------------


def _row_to_project(row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        root=row["root"],
        description=row["description"] or "",
        project_context=row["project_context"] or "",
        created_at=row["created_at"],
        deleted_at=row["deleted_at"],
    )
