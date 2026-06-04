"""REST endpoints for projects (CRUD) + per-project filesystem ops."""

import io
from pathlib import Path

from fastapi.testclient import TestClient

from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.server import create_app


H = {"Authorization": "Bearer x"}


def make_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={
            "scribe": AgentConfig(name="scribe", provider="a", model="m", workspace=ws)
        },
    )


def _client(ark_home, tmp_path):
    return TestClient(create_app(make_config(tmp_path)))


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def test_create_get_list_project(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.post("/projects", headers=H, json={"name": "alpha"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "alpha"
    assert p["deleted_at"] is None
    assert Path(p["root"]).is_dir()

    r = client.get(f"/projects/{p['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == p["id"]

    r = client.get("/projects", headers=H)
    assert r.status_code == 200
    assert [proj["id"] for proj in r.json()] == [p["id"]]


def test_create_rejects_empty_name(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.post("/projects", headers=H, json={"name": ""})
    assert r.status_code == 400


def test_create_rejects_duplicate_active_name(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    client.post("/projects", headers=H, json={"name": "dup"})
    r = client.post("/projects", headers=H, json={"name": "dup"})
    assert r.status_code == 400


def test_create_with_explicit_root(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    target = tmp_path / "my-proj"
    r = client.post(
        "/projects", headers=H, json={"name": "explicit", "root": str(target)}
    )
    assert r.status_code == 200
    assert r.json()["root"] == str(target.resolve())
    assert target.is_dir()


def test_update_project_fields(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    pid = client.post(
        "/projects", headers=H, json={"name": "p", "description": "old"}
    ).json()["id"]
    r = client.put(
        f"/projects/{pid}",
        headers=H,
        json={"description": "new", "project_context": "context body"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == "new"
    assert body["project_context"] == "context body"


def test_soft_delete_does_not_touch_files(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = client.post("/projects", headers=H, json={"name": "doomed"}).json()
    # Write a file
    (Path(p["root"]) / "important.txt").write_text("keep me")
    r = client.delete(f"/projects/{p['id']}", headers=H)
    assert r.status_code == 200
    # Files survive
    assert (Path(p["root"]) / "important.txt").read_text() == "keep me"
    # Project is filtered from default list
    assert client.get("/projects", headers=H).json() == []
    # But appears with include_deleted
    full = client.get("/projects?include_deleted=true", headers=H).json()
    assert any(proj["id"] == p["id"] and proj["deleted_at"] is not None for proj in full)
    # Cannot delete again
    r = client.delete(f"/projects/{p['id']}", headers=H)
    assert r.status_code == 404


def test_create_requires_auth(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.post("/projects", json={"name": "x"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Filesystem endpoints
# ---------------------------------------------------------------------------


def _new_project(client, tmp_path, name="fs"):
    root = tmp_path / name
    return client.post(
        "/projects", headers=H, json={"name": name, "root": str(root)}
    ).json()


def test_list_empty_root(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    r = client.get(f"/projects/{p['id']}/files", headers=H)
    assert r.status_code == 200
    assert r.json() == {"path": "", "entries": []}


def test_put_then_get_file(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    r = client.put(
        f"/projects/{p['id']}/files/notes.md",
        headers=H,
        content=b"# notes\nhello world",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "notes.md"
    assert body["size"] == len(b"# notes\nhello world")
    # Read it back
    r = client.get(f"/projects/{p['id']}/files/notes.md", headers=H)
    assert r.status_code == 200
    assert r.content == b"# notes\nhello world"


def test_put_creates_intermediate_dirs(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    r = client.put(
        f"/projects/{p['id']}/files/a/b/c/deep.txt",
        headers=H,
        content=b"deep",
    )
    assert r.status_code == 200
    assert (Path(p["root"]) / "a/b/c/deep.txt").read_bytes() == b"deep"


def test_get_directory_returns_listing(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "a.txt").write_text("a")
    (Path(p["root"]) / "subdir").mkdir()
    (Path(p["root"]) / "subdir" / "nested.txt").write_text("n")
    r = client.get(f"/projects/{p['id']}/files", headers=H)
    assert r.status_code == 200
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    # Dirs sort first, then files; both alphabetical within each group.
    assert names == ["subdir", "a.txt"]
    is_dirs = {e["name"]: e["is_dir"] for e in body["entries"]}
    assert is_dirs == {"subdir": True, "a.txt": False}


def test_delete_file(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    client.put(f"/projects/{p['id']}/files/x.txt", headers=H, content=b"x")
    r = client.delete(f"/projects/{p['id']}/files/x.txt", headers=H)
    assert r.status_code == 200
    assert not (Path(p["root"]) / "x.txt").exists()


def test_delete_non_empty_dir_rejected(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "d").mkdir()
    (Path(p["root"]) / "d" / "f").write_text("")
    r = client.delete(f"/projects/{p['id']}/files/d", headers=H)
    assert r.status_code == 409


def test_mkdir(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    r = client.post(
        f"/projects/{p['id']}/files/newdir?op=mkdir", headers=H
    )
    assert r.status_code == 200
    assert (Path(p["root"]) / "newdir").is_dir()


def test_rename_file_within_same_dir(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "old.txt").write_text("hello")
    r = client.post(
        f"/projects/{p['id']}/files/old.txt?op=rename&dest=new.txt", headers=H
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "from": "old.txt", "to": "new.txt"}
    assert not (Path(p["root"]) / "old.txt").exists()
    assert (Path(p["root"]) / "new.txt").read_text() == "hello"


def test_rename_into_new_subdir(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "a.txt").write_text("a")
    r = client.post(
        f"/projects/{p['id']}/files/a.txt?op=rename&dest=archive/old/a.txt", headers=H
    )
    assert r.status_code == 200
    assert (Path(p["root"]) / "archive/old/a.txt").read_text() == "a"


def test_rename_directory(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "olddir").mkdir()
    (Path(p["root"]) / "olddir" / "inner.txt").write_text("x")
    r = client.post(
        f"/projects/{p['id']}/files/olddir?op=rename&dest=newdir", headers=H
    )
    assert r.status_code == 200
    assert (Path(p["root"]) / "newdir" / "inner.txt").read_text() == "x"


def test_rename_requires_dest(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "a.txt").write_text("x")
    r = client.post(f"/projects/{p['id']}/files/a.txt?op=rename", headers=H)
    assert r.status_code == 400
    assert "dest" in r.text


def test_rename_404_on_missing_source(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    r = client.post(
        f"/projects/{p['id']}/files/no-such.txt?op=rename&dest=other.txt", headers=H
    )
    assert r.status_code == 404


def test_rename_409_on_existing_dest(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "a.txt").write_text("a")
    (Path(p["root"]) / "b.txt").write_text("b")
    r = client.post(
        f"/projects/{p['id']}/files/a.txt?op=rename&dest=b.txt", headers=H
    )
    assert r.status_code == 409


def test_rename_dest_traversal_blocked(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    (Path(p["root"]) / "a.txt").write_text("a")
    r = client.post(
        f"/projects/{p['id']}/files/a.txt?op=rename&dest=../outside.txt", headers=H
    )
    # Resolution catches it before the move runs
    assert r.status_code == 400
    assert (Path(p["root"]) / "a.txt").exists()  # source untouched


def test_filesystem_endpoints_404_on_deleted_project(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    p = _new_project(client, tmp_path)
    client.delete(f"/projects/{p['id']}", headers=H)
    r = client.get(f"/projects/{p['id']}/files", headers=H)
    assert r.status_code == 404


def test_filesystem_never_leaks_outside_root(ark_home, tmp_path):
    """Strong invariant: regardless of URL encoding, no file outside the
    project root is readable through the filesystem endpoint."""
    client = _client(ark_home, tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("HIDDEN-CONTENT")
    p = _new_project(client, tmp_path, name="confined")
    attempts = [
        "../secret.txt",
        "..%2Fsecret.txt",
        "%2e%2e%2Fsecret.txt",
        "a/..%2F..%2Fsecret.txt",
        "/" + str(secret),
    ]
    for path in attempts:
        r = client.get(f"/projects/{p['id']}/files/{path}", headers=H)
        assert r.status_code in (400, 404), (path, r.status_code)
        assert "HIDDEN-CONTENT" not in r.text, f"leaked via {path!r}"
