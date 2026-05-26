"""How projects interact with the runtime: session binding, system prompt
layering, upload routing."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ark import db, projects, runtime, tools
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.server import create_app
from ark.tools import ToolContext


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
# Session creation
# ---------------------------------------------------------------------------


def test_session_create_with_project_id_binds(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    proj = client.post("/projects", headers=H, json={"name": "bind"}).json()
    sid = client.post(
        "/agents/scribe/sessions", headers=H, json={"project_id": proj["id"]}
    ).json()["id"]
    # session_project reflects the binding
    conn = client.app.state.conn
    p = runtime.session_project(conn, sid)
    assert p is not None and p.id == proj["id"]


def test_session_create_without_project(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    conn = client.app.state.conn
    assert runtime.session_project(conn, sid) is None


def test_session_create_rejects_unknown_project(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.post(
        "/agents/scribe/sessions", headers=H, json={"project_id": "no-such-project"}
    )
    assert r.status_code == 400


def test_session_create_rejects_deleted_project(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    proj = client.post("/projects", headers=H, json={"name": "doomed"}).json()
    client.delete(f"/projects/{proj['id']}", headers=H)
    r = client.post(
        "/agents/scribe/sessions", headers=H, json={"project_id": proj["id"]}
    )
    assert r.status_code == 400


def test_session_project_returns_none_when_project_soft_deleted(ark_home, tmp_path):
    """Soft-deleting a project shouldn't break sessions, but the runtime treats
    them as project-less from that point so we don't keep layering project
    context onto a stale project."""
    client = _client(ark_home, tmp_path)
    proj = client.post("/projects", headers=H, json={"name": "later-deleted"}).json()
    sid = client.post(
        "/agents/scribe/sessions", headers=H, json={"project_id": proj["id"]}
    ).json()["id"]
    client.delete(f"/projects/{proj['id']}", headers=H)
    conn = client.app.state.conn
    assert runtime.session_project(conn, sid) is None


# ---------------------------------------------------------------------------
# System prompt layering
# ---------------------------------------------------------------------------


def test_system_prompt_includes_project_section(tmp_path, ark_home):
    from ark.types import Project, SessionContext

    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = AgentConfig(name="scribe", provider="a", model="m", workspace=workspace)
    project = Project(
        id="abc",
        name="marketing",
        root=str(tmp_path / "proj"),
        description="Q4 brochure",
        project_context="Tone: warm but professional.",
        created_at=0,
    )
    prompt = runtime.system_prompt(agent, contexts=[], project=project)
    assert "Project (this session)" in prompt
    assert "marketing" in prompt
    assert str(tmp_path / "proj") in prompt
    assert "Q4 brochure" in prompt
    assert "Tone: warm but professional." in prompt
    # Environment section still present
    assert "Environment" in prompt
    # Ordering: agent prelude → Environment → Project → (no session context)
    assert prompt.index("Environment") < prompt.index("Project (this session)")


def test_system_prompt_omits_project_section_when_no_project(tmp_path, ark_home):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = AgentConfig(name="scribe", provider="a", model="m", workspace=workspace)
    prompt = runtime.system_prompt(agent)
    assert "Project (this session)" not in prompt


# ---------------------------------------------------------------------------
# Uploads route to project when bound
# ---------------------------------------------------------------------------


def test_upload_in_project_session_lands_in_project_root(ark_home, tmp_path):
    import io

    client = _client(ark_home, tmp_path)
    proj = client.post(
        "/projects", headers=H, json={"name": "up", "root": str(tmp_path / "up")}
    ).json()
    sid = client.post(
        "/agents/scribe/sessions", headers=H, json={"project_id": proj["id"]}
    ).json()["id"]
    r = client.post(
        f"/agents/scribe/sessions/{sid}/uploads",
        headers=H,
        files={"file": ("hello.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert r.status_code == 200
    # File should land in the project root's uploads/ dir, not the agent workspace
    assert (Path(proj["root"]) / "uploads" / "hello.txt").read_bytes() == b"hi"
    # And NOT in the agent workspace
    workspace_uploads = tmp_path / "ws" / "uploads" / "hello.txt"
    assert not workspace_uploads.exists()


def test_upload_in_non_project_session_lands_in_workspace(ark_home, tmp_path):
    import io

    client = _client(ark_home, tmp_path)
    sid = client.post("/agents/scribe/sessions", headers=H).json()["id"]
    r = client.post(
        f"/agents/scribe/sessions/{sid}/uploads",
        headers=H,
        files={"file": ("hello.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert r.status_code == 200
    workspace = tmp_path / "ws"
    assert (workspace / "uploads" / "hello.txt").read_bytes() == b"hi"


def test_list_uploads_endpoint_dispatches(ark_home, tmp_path):
    import io

    client = _client(ark_home, tmp_path)
    proj = client.post(
        "/projects", headers=H, json={"name": "ldis", "root": str(tmp_path / "ldis")}
    ).json()
    sid_proj = client.post(
        "/agents/scribe/sessions", headers=H, json={"project_id": proj["id"]}
    ).json()["id"]
    sid_no = client.post("/agents/scribe/sessions", headers=H).json()["id"]

    # Upload in each
    client.post(
        f"/agents/scribe/sessions/{sid_proj}/uploads",
        headers=H,
        files={"file": ("project-file.txt", io.BytesIO(b"p"), "text/plain")},
    )
    client.post(
        f"/agents/scribe/sessions/{sid_no}/uploads",
        headers=H,
        files={"file": ("workspace-file.txt", io.BytesIO(b"w"), "text/plain")},
    )

    proj_list = client.get(
        f"/agents/scribe/sessions/{sid_proj}/uploads", headers=H
    ).json()
    no_list = client.get(
        f"/agents/scribe/sessions/{sid_no}/uploads", headers=H
    ).json()

    proj_names = [e["path"] for e in proj_list]
    no_names = [e["path"] for e in no_list]
    assert "uploads/project-file.txt" in proj_names
    assert "uploads/workspace-file.txt" not in proj_names
    assert "uploads/workspace-file.txt" in no_names
    assert "uploads/project-file.txt" not in no_names


# ---------------------------------------------------------------------------
# Agent tools: get_current_session_info + get_project_info
# ---------------------------------------------------------------------------


def make_ctx(conn, session_id, agent_workspace, tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent = AgentConfig(name="scribe", provider="a", model="m", workspace=cwd)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"scribe": agent},
    )
    return ToolContext(
        conn=conn,
        config=cfg,
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        loaded_skills=set(),
    )


def test_get_current_session_info_includes_project_when_bound(ark_home, tmp_path):
    import json

    conn = db.init_db()
    p = projects.create(conn, name="info", root=str(tmp_path / "info"))
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=p.id)
    ctx = make_ctx(conn, sid, tmp_path / "ws", tmp_path)
    out, err = asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))
    assert err is False, out
    info = json.loads(out)
    assert info["project_id"] == p.id
    assert info["project_name"] == "info"
    assert info["project_root"] == p.root


def test_get_current_session_info_no_project(ark_home, tmp_path):
    import json

    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, sid, tmp_path / "ws", tmp_path)
    out, err = asyncio.run(tools.execute("get_current_session_info", {}, ctx=ctx))
    info = json.loads(out)
    assert info["project_id"] is None
    assert info["project_name"] is None
    assert info["project_root"] is None


def test_get_project_info_returns_project_metadata(ark_home, tmp_path):
    import json

    conn = db.init_db()
    p = projects.create(
        conn,
        name="meta",
        root=str(tmp_path / "meta"),
        description="for tests",
        project_context="be terse",
    )
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=p.id)
    ctx = make_ctx(conn, sid, tmp_path / "ws", tmp_path)
    out, err = asyncio.run(tools.execute("get_project_info", {}, ctx=ctx))
    info = json.loads(out)
    assert info["id"] == p.id
    assert info["name"] == "meta"
    assert info["description"] == "for tests"
    assert info["project_context"] == "be terse"


def test_get_project_info_null_when_no_project(ark_home, tmp_path):
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")
    ctx = make_ctx(conn, sid, tmp_path / "ws", tmp_path)
    out, err = asyncio.run(tools.execute("get_project_info", {}, ctx=ctx))
    # _to_str renders None as empty string
    assert out == ""
