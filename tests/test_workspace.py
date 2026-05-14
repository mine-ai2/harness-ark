"""Workspace path helpers — traversal guard + auto-suffix."""

import pytest

from ark import workspace as ws


def test_resolve_simple(tmp_path):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "a.txt").write_text("x")
    full = ws.resolve(tmp_path, "uploads/a.txt")
    assert full == (tmp_path / "uploads" / "a.txt").resolve()


def test_resolve_rejects_absolute(tmp_path):
    with pytest.raises(ws.WorkspaceError, match="workspace-relative"):
        ws.resolve(tmp_path, "/etc/passwd")


def test_resolve_rejects_traversal(tmp_path):
    with pytest.raises(ws.WorkspaceError, match="escapes workspace"):
        ws.resolve(tmp_path, "../../etc/passwd")


def test_resolve_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    try:
        (tmp_path / "link").symlink_to(outside)
        with pytest.raises(ws.WorkspaceError, match="escapes workspace"):
            ws.resolve(tmp_path, "link")
    finally:
        outside.unlink(missing_ok=True)


def test_reserve_upload_no_collision(tmp_path):
    p = ws.reserve_upload_filename(tmp_path, "report.pdf")
    assert p == tmp_path / "uploads" / "report.pdf"
    assert p.parent.is_dir()


def test_reserve_upload_auto_suffix(tmp_path):
    p1 = ws.reserve_upload_filename(tmp_path, "report.pdf")
    p1.write_text("first")
    p2 = ws.reserve_upload_filename(tmp_path, "report.pdf")
    assert p2.name == "report-2.pdf"
    p2.write_text("second")
    p3 = ws.reserve_upload_filename(tmp_path, "report.pdf")
    assert p3.name == "report-3.pdf"


def test_reserve_strips_path_components(tmp_path):
    p = ws.reserve_upload_filename(tmp_path, "../../etc/passwd")
    assert p == tmp_path / "uploads" / "passwd"


def test_reserve_rejects_dotdot_or_empty(tmp_path):
    with pytest.raises(ws.WorkspaceError):
        ws.reserve_upload_filename(tmp_path, "..")
    with pytest.raises(ws.WorkspaceError):
        ws.reserve_upload_filename(tmp_path, "")
