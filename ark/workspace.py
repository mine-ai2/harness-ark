"""Workspace path helpers.

Centralizes everything path-related for an agent's workspace — uploads dir
resolution, traversal-safe relative-path resolution, and auto-suffixed
filename reservation for upload collisions.

All paths returned by `resolve` are guaranteed to live under the workspace
root. Anything that would escape (via `..`, absolute paths, symlinks) raises
`WorkspaceError`.
"""

from __future__ import annotations

from pathlib import Path

UPLOADS_DIRNAME = "uploads"


class WorkspaceError(Exception):
    pass


def uploads_dir(workspace: Path) -> Path:
    return workspace / UPLOADS_DIRNAME


def ensure_uploads_dir(workspace: Path) -> Path:
    d = uploads_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve(workspace: Path, relative: str) -> Path:
    """Resolve a workspace-relative path, guarding against traversal.

    Returns the absolute path. Raises WorkspaceError if the resolved path
    would land outside the workspace root (including via symlinks).
    """

    workspace_real = workspace.resolve()
    # Reject absolute inputs outright.
    candidate = Path(relative)
    if candidate.is_absolute():
        raise WorkspaceError(f"path must be workspace-relative: {relative!r}")
    full = (workspace_real / candidate).resolve()
    try:
        full.relative_to(workspace_real)
    except ValueError:
        raise WorkspaceError(f"path escapes workspace: {relative!r}")
    return full


def relative_to_workspace(workspace: Path, full: Path) -> str:
    workspace_real = workspace.resolve()
    return str(full.resolve().relative_to(workspace_real))


def reserve_upload_filename(workspace: Path, original: str) -> Path:
    """Pick a non-colliding absolute path for an upload.

    `original` is sanitized to a bare filename (no directory components). If
    a file with that name already exists in the uploads dir, a `-N` suffix
    is appended before the extension until an unused name is found.
    """

    base = Path(original).name  # strip any path components from user input
    if not base or base in (".", ".."):
        raise WorkspaceError(f"invalid filename: {original!r}")
    uploads = ensure_uploads_dir(workspace)
    candidate = uploads / base
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 2
    while True:
        c = uploads / f"{stem}-{i}{suffix}"
        if not c.exists():
            return c
        i += 1
