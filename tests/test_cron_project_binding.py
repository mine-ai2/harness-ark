"""Cron entries can be bound to a project. Covers the schema migration, the
scheduler firing project-aware sessions, the REST endpoints, the agent tool
signature, and the list_projects tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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


def _make_project(client, name, tmp_path):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    r = client.post("/projects", headers=H, json={"name": name, "root": str(root)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tool_ctx(tmp_path, cfg, conn):
    return ToolContext(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id="s",
        cwd=tmp_path,
        loaded_skills=set(),
    )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_crons_table_has_project_id_column(ark_home, tmp_path):
    conn = db.init_db()
    cols = [row[1] for row in conn.execute("PRAGMA table_info(crons)").fetchall()]
    assert "project_id" in cols


# ---------------------------------------------------------------------------
# REST: PUT / GET
# ---------------------------------------------------------------------------


def test_put_cron_with_project_id(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    pid = _make_project(client, "alpha", tmp_path)

    r = client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do it", "project_id": pid},
    )
    assert r.status_code == 200, r.text

    row = conn.execute(
        "SELECT project_id FROM crons WHERE agent_name = ? AND id = ?",
        ("scribe", "morning"),
    ).fetchone()
    assert row["project_id"] == pid


def test_put_cron_omitting_project_id_preserves_existing(ark_home, tmp_path):
    """PUT without project_id must not clobber an existing binding — this is
    the "update expr only" workflow."""
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    pid = _make_project(client, "alpha", tmp_path)

    client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do it", "project_id": pid},
    )
    # Update expr + prompt, omit project_id:
    r = client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 10 * * *", "prompt": "do it later"},
    )
    assert r.status_code == 200
    row = conn.execute(
        "SELECT expr, project_id FROM crons WHERE id = 'morning'"
    ).fetchone()
    assert row["expr"] == "0 10 * * *"
    assert row["project_id"] == pid  # preserved


def test_put_cron_project_id_null_detaches(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    conn = client.app.state.conn
    pid = _make_project(client, "alpha", tmp_path)
    client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do it", "project_id": pid},
    )
    r = client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do it", "project_id": None},
    )
    assert r.status_code == 200
    row = conn.execute(
        "SELECT project_id FROM crons WHERE id = 'morning'"
    ).fetchone()
    assert row["project_id"] is None


def test_put_cron_unknown_project_404(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do", "project_id": "no-such"},
    )
    assert r.status_code == 404
    assert "unknown project" in r.text


def test_put_cron_soft_deleted_project_404(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    pid = _make_project(client, "alpha", tmp_path)
    client.delete(f"/projects/{pid}", headers=H)  # soft-delete
    r = client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do", "project_id": pid},
    )
    assert r.status_code == 404


def test_put_cron_non_string_project_id_400(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    r = client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do", "project_id": 42},
    )
    assert r.status_code == 400


def test_get_crons_returns_project_info(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    pid = _make_project(client, "alpha", tmp_path)
    client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do", "project_id": pid},
    )
    r = client.get("/agents/scribe/crons", headers=H)
    row = [c for c in r.json() if c["id"] == "morning"][0]
    assert row["project_id"] == pid
    assert row["project_name"] == "alpha"


def test_get_crons_null_project_when_unbound(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do"},
    )
    r = client.get("/agents/scribe/crons", headers=H)
    row = [c for c in r.json() if c["id"] == "morning"][0]
    assert row["project_id"] is None
    assert row["project_name"] is None


# ---------------------------------------------------------------------------
# Agent tool: add_cron accepts project_id
# ---------------------------------------------------------------------------


import asyncio


def _run(name, args, ctx):
    return asyncio.run(tools.execute(name, args, ctx=ctx))


def test_add_cron_tool_with_project_id(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    p = projects.create(
        conn, name="alpha", root=str(tmp_path / "alpha"),
        description="", project_context="",
    )
    ctx = _tool_ctx(tmp_path, cfg, conn)

    output, err = _run(
        "add_cron",
        {"id": "morning", "expr": "0 9 * * *", "prompt": "do", "project_id": p.id},
        ctx,
    )
    assert err is False
    assert p.id in output

    row = conn.execute(
        "SELECT project_id FROM crons WHERE id = 'morning'"
    ).fetchone()
    assert row["project_id"] == p.id


def test_add_cron_tool_rejects_unknown_project(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    ctx = _tool_ctx(tmp_path, cfg, conn)

    output, err = _run(
        "add_cron",
        {"id": "x", "expr": "* * * * *", "prompt": "do", "project_id": "not-a-project"},
        ctx,
    )
    assert err is True
    assert "unknown project" in output


def test_add_cron_tool_without_project_id_still_works(ark_home, tmp_path):
    """Backwards-compatible: existing add_cron callers don't need to change."""
    cfg = make_config(tmp_path)
    conn = db.init_db()
    ctx = _tool_ctx(tmp_path, cfg, conn)

    output, err = _run(
        "add_cron",
        {"id": "morning", "expr": "0 9 * * *", "prompt": "do"},
        ctx,
    )
    assert err is False
    row = conn.execute(
        "SELECT project_id FROM crons WHERE id = 'morning'"
    ).fetchone()
    assert row["project_id"] is None


def test_list_crons_shows_project_binding(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    p = projects.create(
        conn, name="alpha", root=str(tmp_path / "alpha"),
        description="", project_context="",
    )
    conn.execute(
        "INSERT INTO crons(agent_name, id, expr, prompt, enabled, project_id) "
        "VALUES ('scribe','morning','0 9 * * *','do',1,?)",
        (p.id,),
    )
    ctx = _tool_ctx(tmp_path, cfg, conn)
    output, err = _run("list_crons", {}, ctx)
    assert err is False
    assert "project=alpha" in output


def test_list_crons_flags_deleted_project(ark_home, tmp_path):
    """A cron bound to a soft-deleted project shows DELETED so the operator/
    agent can see why fires aren't landing in the project."""
    cfg = make_config(tmp_path)
    conn = db.init_db()
    p = projects.create(
        conn, name="alpha", root=str(tmp_path / "alpha"),
        description="", project_context="",
    )
    conn.execute(
        "INSERT INTO crons(agent_name, id, expr, prompt, enabled, project_id) "
        "VALUES ('scribe','morning','0 9 * * *','do',1,?)",
        (p.id,),
    )
    projects.soft_delete(conn, p.id)

    ctx = _tool_ctx(tmp_path, cfg, conn)
    output, err = _run("list_crons", {}, ctx)
    assert err is False
    assert "DELETED" in output


# ---------------------------------------------------------------------------
# Agent tool: list_projects
# ---------------------------------------------------------------------------


def test_list_projects_returns_active_only(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    p1 = projects.create(
        conn, name="alpha", root=str(tmp_path / "alpha"),
        description="Alpha desc", project_context="",
    )
    p2 = projects.create(
        conn, name="beta", root=str(tmp_path / "beta"),
        description="", project_context="",
    )
    projects.soft_delete(conn, p2.id)

    ctx = _tool_ctx(tmp_path, cfg, conn)
    output, err = _run("list_projects", {}, ctx)
    assert err is False
    # Output is JSON-encoded list of dicts (tools serialize via _to_str).
    import json
    parsed = json.loads(output)
    names = {p["name"] for p in parsed}
    assert names == {"alpha"}  # beta is soft-deleted, excluded
    only = parsed[0]
    assert only["id"] == p1.id
    assert only["root"].endswith("alpha")
    assert only["description"] == "Alpha desc"


def test_list_projects_empty(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    ctx = _tool_ctx(tmp_path, cfg, conn)
    output, err = _run("list_projects", {}, ctx)
    assert err is False
    import json
    assert json.loads(output) == []


# ---------------------------------------------------------------------------
# Scheduler: fires create sessions bound to the cron's project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_fire_creates_project_bound_session(ark_home, tmp_path):
    """End-to-end at the runtime layer: fake a cron fire by calling _drive
    directly, and confirm the session row has the cron's project_id."""
    import ark.scheduler as sched_module

    cfg = make_config(tmp_path)
    conn = db.init_db()
    p = projects.create(
        conn, name="alpha", root=str(tmp_path / "alpha"),
        description="", project_context="",
    )

    scheduler = sched_module.Scheduler(conn, cfg)

    # Stub out the actual turn drive so we don't hit a provider — we only
    # care about session creation.
    async def _noop_run_and_publish(**_kw):
        return None

    import ark.runtime as _runtime
    orig = _runtime.run_and_publish
    _runtime.run_and_publish = _noop_run_and_publish
    try:
        await scheduler._fire_cron("scribe", "morning", "do", p.id)
    finally:
        _runtime.run_and_publish = orig

    row = conn.execute(
        "SELECT project_id, cron_id, kind FROM sessions "
        "WHERE agent_name = 'scribe' AND cron_id = 'morning'"
    ).fetchone()
    assert row is not None
    assert row["kind"] == "cron"
    assert row["project_id"] == p.id
    assert row["cron_id"] == "morning"


@pytest.mark.asyncio
async def test_scheduler_fire_with_deleted_project_still_creates_session(
    ark_home, tmp_path, capsys
):
    """A cron whose project got soft-deleted between definition and fire time:
    session is still created (with the dangling project_id recorded), and a
    warning is logged to stderr. runtime.session_project returns None for the
    session so it just runs project-less."""
    import ark.scheduler as sched_module

    cfg = make_config(tmp_path)
    conn = db.init_db()
    p = projects.create(
        conn, name="alpha", root=str(tmp_path / "alpha"),
        description="", project_context="",
    )
    projects.soft_delete(conn, p.id)

    scheduler = sched_module.Scheduler(conn, cfg)

    async def _noop_run_and_publish(**_kw):
        return None

    import ark.runtime as _runtime
    orig = _runtime.run_and_publish
    _runtime.run_and_publish = _noop_run_and_publish
    try:
        await scheduler._fire_cron("scribe", "morning", "do", p.id)
    finally:
        _runtime.run_and_publish = orig

    err = capsys.readouterr().err
    assert "deleted" in err
    assert "morning" in err

    # Session row records the dangling project_id (audit) but the runtime
    # helper treats it as project-less.
    row = conn.execute(
        "SELECT project_id FROM sessions WHERE cron_id = 'morning'"
    ).fetchone()
    assert row["project_id"] == p.id
    assert runtime.session_project(conn, conn.execute(
        "SELECT id FROM sessions WHERE cron_id = 'morning'"
    ).fetchone()["id"]) is None


# ---------------------------------------------------------------------------
# GET /agents/{name} surfaces project_id on crons
# ---------------------------------------------------------------------------


def test_agent_detail_endpoint_includes_project_id_on_crons(ark_home, tmp_path):
    client = _client(ark_home, tmp_path)
    pid = _make_project(client, "alpha", tmp_path)
    client.put(
        "/agents/scribe/crons/morning",
        headers=H,
        json={"expr": "0 9 * * *", "prompt": "do", "project_id": pid},
    )
    r = client.get("/agents/scribe", headers=H)
    assert r.status_code == 200
    match = [c for c in r.json()["crons"] if c["id"] == "morning"][0]
    assert match["project_id"] == pid
