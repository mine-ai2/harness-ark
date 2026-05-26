"""REST filesystem endpoints for agent workspaces.

Mirrors the project filesystem endpoints (see test_projects_rest.py) but
scoped to an agent's workspace under `/agents/{name}/files/...`. The
existing download-only behavior of `GET /agents/{name}/files/{path}` is
preserved when the target is a file; directories now return a JSON listing.
"""

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
    return TestClient(create_app(make_config(tmp_path))), tmp_path / "ws"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_empty_workspace_root(ark_home, tmp_path):
    client, _ = _client(ark_home, tmp_path)
    r = client.get("/agents/scribe/files", headers=H)
    assert r.status_code == 200
    assert r.json() == {"path": "", "entries": []}


def test_list_workspace_root_with_files(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    (ws / "a.txt").write_text("a")
    (ws / "b.txt").write_text("bb")
    (ws / "sub").mkdir()
    r = client.get("/agents/scribe/files", headers=H)
    assert r.status_code == 200
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    # Dirs first, then files (alphabetical within each)
    assert names == ["sub", "a.txt", "b.txt"]
    is_dirs = {e["name"]: e["is_dir"] for e in body["entries"]}
    assert is_dirs == {"sub": True, "a.txt": False, "b.txt": False}


def test_get_directory_returns_listing(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    (ws / "nested").mkdir()
    (ws / "nested" / "x.txt").write_text("hi")
    r = client.get("/agents/scribe/files/nested", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "nested"
    assert [e["name"] for e in body["entries"]] == ["x.txt"]


# ---------------------------------------------------------------------------
# Read / write / delete
# ---------------------------------------------------------------------------


def test_put_then_get_file(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    r = client.put(
        "/agents/scribe/files/notes.md", headers=H, content=b"# notes\nworkspace file"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "notes.md"
    assert body["size"] == len(b"# notes\nworkspace file")
    assert (ws / "notes.md").read_bytes() == b"# notes\nworkspace file"
    # Round-trip read
    r = client.get("/agents/scribe/files/notes.md", headers=H)
    assert r.status_code == 200
    assert r.content == b"# notes\nworkspace file"


def test_put_creates_intermediate_dirs(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    r = client.put(
        "/agents/scribe/files/a/b/c/deep.txt", headers=H, content=b"deep"
    )
    assert r.status_code == 200
    assert (ws / "a/b/c/deep.txt").read_bytes() == b"deep"


def test_delete_file(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    (ws / "doomed.txt").write_text("bye")
    r = client.delete("/agents/scribe/files/doomed.txt", headers=H)
    assert r.status_code == 200
    assert not (ws / "doomed.txt").exists()


def test_delete_non_empty_dir_rejected(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    (ws / "d").mkdir()
    (ws / "d" / "f").write_text("")
    r = client.delete("/agents/scribe/files/d", headers=H)
    assert r.status_code == 409


def test_mkdir(ark_home, tmp_path):
    client, ws = _client(ark_home, tmp_path)
    r = client.post("/agents/scribe/files/scratch?op=mkdir", headers=H)
    assert r.status_code == 200
    assert (ws / "scratch").is_dir()


def test_post_unknown_op(ark_home, tmp_path):
    client, _ = _client(ark_home, tmp_path)
    r = client.post("/agents/scribe/files/x?op=banana", headers=H)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth + unknown agent
# ---------------------------------------------------------------------------


def test_list_requires_auth(ark_home, tmp_path):
    client, _ = _client(ark_home, tmp_path)
    r = client.get("/agents/scribe/files")
    assert r.status_code == 401


def test_unknown_agent_404(ark_home, tmp_path):
    client, _ = _client(ark_home, tmp_path)
    r = client.get("/agents/no-such-agent/files", headers=H)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Path traversal — strong invariant
# ---------------------------------------------------------------------------


def test_filesystem_never_leaks_outside_workspace(ark_home, tmp_path):
    """Strong invariant: any URL traversal trick must not yield bytes from
    outside the workspace, on any verb."""
    client, _ = _client(ark_home, tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("HIDDEN-CONTENT")
    attempts = [
        "../secret.txt",
        "..%2Fsecret.txt",
        "%2e%2e%2Fsecret.txt",
        "a/..%2F..%2Fsecret.txt",
        "/" + str(secret),
    ]
    for path in attempts:
        r = client.get(f"/agents/scribe/files/{path}", headers=H)
        assert r.status_code in (400, 404), (path, r.status_code)
        assert "HIDDEN-CONTENT" not in r.text, f"leaked via {path!r}"
