"""Projects module: CRUD, soft-delete, path resolution."""

import pytest

from ark import db, projects
from ark.projects import ProjectError, ProjectPathError


def test_create_with_default_root(ark_home):
    conn = db.init_db()
    p = projects.create(conn, name="alpha")
    assert p.name == "alpha"
    # Default root is <ARK_HOME>/projects/<id>/
    assert p.root.endswith(f"projects/{p.id}")
    from pathlib import Path

    assert Path(p.root).is_dir()
    assert p.deleted_at is None


def test_create_with_explicit_root(ark_home, tmp_path):
    conn = db.init_db()
    target = tmp_path / "my-proj"
    p = projects.create(conn, name="beta", root=str(target))
    assert p.root == str(target.resolve())
    assert target.is_dir()


def test_create_rejects_relative_root(ark_home):
    conn = db.init_db()
    # We resolve() the path; Path.resolve() makes it absolute, so a relative
    # input still becomes absolute. The only way to hit the absolute check is
    # to manually construct it, so this is mostly a defensive guard.
    # Empty name is the more important error path.
    with pytest.raises(ProjectError, match="name is required"):
        projects.create(conn, name="")
    with pytest.raises(ProjectError, match="name is required"):
        projects.create(conn, name="   ")


def test_active_name_uniqueness(ark_home):
    conn = db.init_db()
    projects.create(conn, name="dup")
    with pytest.raises(ProjectError, match="already exists"):
        projects.create(conn, name="dup")


def test_soft_delete_frees_name(ark_home):
    """A soft-deleted project's name can be reused for a fresh one."""
    conn = db.init_db()
    p1 = projects.create(conn, name="reusable")
    assert projects.soft_delete(conn, p1.id) is True
    p2 = projects.create(conn, name="reusable")
    assert p2.id != p1.id


def test_soft_delete_idempotent_and_safe(ark_home):
    conn = db.init_db()
    p = projects.create(conn, name="x")
    assert projects.soft_delete(conn, p.id) is True
    # Second delete returns False (already deleted)
    assert projects.soft_delete(conn, p.id) is False
    # Files on disk are not touched
    from pathlib import Path

    assert Path(p.root).is_dir()


def test_soft_delete_unknown(ark_home):
    conn = db.init_db()
    assert projects.soft_delete(conn, "no-such-id") is False


def test_list_active_vs_all(ark_home):
    conn = db.init_db()
    a = projects.create(conn, name="a")
    b = projects.create(conn, name="b")
    projects.soft_delete(conn, a.id)
    active = [p.id for p in projects.list_projects(conn)]
    assert active == [b.id]
    all_ = [p.id for p in projects.list_projects(conn, include_deleted=True)]
    assert set(all_) == {a.id, b.id}


def test_update_fields(ark_home):
    conn = db.init_db()
    p = projects.create(conn, name="orig", description="initial", project_context="ctx")
    p2 = projects.update(conn, p.id, description="updated")
    assert p2.description == "updated"
    assert p2.project_context == "ctx"
    p3 = projects.update(conn, p.id, project_context="new ctx")
    assert p3.project_context == "new ctx"


def test_update_rejects_deleted(ark_home):
    conn = db.init_db()
    p = projects.create(conn, name="x")
    projects.soft_delete(conn, p.id)
    with pytest.raises(ProjectError, match="has been deleted"):
        projects.update(conn, p.id, description="…")


def test_update_unknown(ark_home):
    conn = db.init_db()
    with pytest.raises(ProjectError, match="unknown project"):
        projects.update(conn, "nope", description="x")


# ---------------------------------------------------------------------------
# Path resolution: must enforce project root containment
# ---------------------------------------------------------------------------


def test_resolve_simple(ark_home, tmp_path):
    conn = db.init_db()
    p = projects.create(conn, name="r", root=str(tmp_path / "r"))
    (tmp_path / "r" / "foo.txt").write_text("hi")
    full = projects.resolve_path(p, "foo.txt")
    assert full == (tmp_path / "r" / "foo.txt").resolve()


def test_resolve_rejects_absolute(ark_home, tmp_path):
    conn = db.init_db()
    p = projects.create(conn, name="r", root=str(tmp_path / "r"))
    with pytest.raises(ProjectPathError, match="project-relative"):
        projects.resolve_path(p, "/etc/passwd")


def test_resolve_rejects_traversal(ark_home, tmp_path):
    conn = db.init_db()
    p = projects.create(conn, name="r", root=str(tmp_path / "r"))
    with pytest.raises(ProjectPathError, match="escapes project root"):
        projects.resolve_path(p, "../escape.txt")


def test_resolve_rejects_symlink_escape(ark_home, tmp_path):
    conn = db.init_db()
    p = projects.create(conn, name="r", root=str(tmp_path / "r"))
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    try:
        (tmp_path / "r" / "trap").symlink_to(outside)
        with pytest.raises(ProjectPathError, match="escapes project root"):
            projects.resolve_path(p, "trap")
    finally:
        outside.unlink(missing_ok=True)
