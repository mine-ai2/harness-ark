"""One file root for project-bound sessions (mine-capstone#602).

Ark bound tool execution to the AGENT WORKSPACE unconditionally while every
MineAI surface — uploads, `workspace_files.*`, the files panel, the download
proxy — reads the ARK PROJECT ROOT for project-bound sessions. A file the
agent wrote was invisible, and `share_with_client` published a
workspace-relative path the proxy then 404'd on.

These tests pin both halves of the fix: the turn loop's `cwd` follows the
session's project, and `share_with_client` publishes the path against the
same root (falling back to the other root so pre-fix paths still resolve).
"""

import asyncio
from pathlib import Path

import pytest

from ark import db, projects, runtime, tools
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.tools import ToolContext
from ark.types import AssistantTurnEnd, SharedFile, ToolCallEvent


def make_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    agent = AgentConfig(name="scribe", provider="a", model="m", workspace=ws)
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"scribe": agent},
    )


class _ToolThenStop:
    """Emits one tool call on the first turn, then ends."""

    def __init__(self, name, args):
        self.name, self.args = name, args
        self.turns = 0

    async def stream_turn(self, *, model, system, messages, tools, max_tokens=4096):
        self.turns += 1
        if self.turns == 1:
            yield ToolCallEvent(id="tc1", name=self.name, input=self.args)
            yield AssistantTurnEnd(text="", stop_reason="tool_use")
        else:
            yield AssistantTurnEnd(text="done", stop_reason="end_turn")


async def _run(cfg, conn, sid, provider):
    return [
        evt
        async for evt in runtime.run_user_turn(
            conn=conn,
            config=cfg,
            agent=cfg.agents["scribe"],
            session_id=sid,
            user_text="write it",
            provider_factory=lambda *_a, **_k: provider,
        )
    ]


# ---------------------------------------------------------------------------
# cwd binding per session kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_in_project_session_lands_in_project_root(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="p", root=str(tmp_path / "proj"))
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)

    await _run(
        cfg,
        conn,
        sid,
        _ToolThenStop("write_file", {"path": "report.txt", "content": "hi"}),
    )

    # Visible in the shared project tree the MineAI files panel reads...
    assert (Path(proj.root) / "report.txt").read_text() == "hi"
    # ...and NOT stranded in the agent workspace.
    assert not (tmp_path / "ws" / "report.txt").exists()


@pytest.mark.asyncio
async def test_write_file_in_unbound_session_lands_in_workspace(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    sid = runtime.create_session(conn, "scribe", "conversational")

    await _run(
        cfg,
        conn,
        sid,
        _ToolThenStop("write_file", {"path": "report.txt", "content": "hi"}),
    )

    assert (tmp_path / "ws" / "report.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_project_session_with_deleted_project_falls_back_to_workspace(
    ark_home, tmp_path
):
    """A soft-deleted project must not strand the turn — `session_project`
    returns None and the workspace takes over, same as an unbound session."""

    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="gone", root=str(tmp_path / "gone"))
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)
    projects.soft_delete(conn, proj.id)

    await _run(
        cfg,
        conn,
        sid,
        _ToolThenStop("write_file", {"path": "report.txt", "content": "hi"}),
    )

    assert (tmp_path / "ws" / "report.txt").read_text() == "hi"


# ---------------------------------------------------------------------------
# share_with_client resolves against the same root the proxy reads
# ---------------------------------------------------------------------------


def _ctx(conn, cfg, sid, cwd):
    return ToolContext(
        conn=conn,
        config=cfg,
        agent=cfg.agents["scribe"],
        session_id=sid,
        cwd=cwd,
        loaded_skills=set(),
    )


def test_share_publishes_project_relative_path(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="share", root=str(tmp_path / "share"))
    root = Path(proj.root)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "out" / "deck.pptx").write_bytes(b"PK fake")
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)

    output, err = asyncio.run(
        tools.execute(
            "share_with_client", {"path": "out/deck.pptx"}, ctx=_ctx(conn, cfg, sid, root)
        )
    )
    assert err is False, output

    (shared,) = [m for m in runtime.load_history(conn, sid) if isinstance(m, SharedFile)]
    # Project-root-relative: exactly what the MineAI download proxy passes to
    # `read_project_file` for a bound session.
    assert shared.path == "out/deck.pptx"
    assert shared.size == len(b"PK fake")


def test_share_in_project_session_falls_back_to_workspace(ark_home, tmp_path):
    """Pre-fix habit (or a model reaching into the workspace) still resolves
    instead of erroring — the file just isn't in the project tree."""

    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="fb", root=str(tmp_path / "fb"))
    Path(proj.root).mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "legacy.csv").write_text("a,b")
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)

    output, err = asyncio.run(
        tools.execute(
            "share_with_client",
            {"path": "legacy.csv"},
            ctx=_ctx(conn, cfg, sid, Path(proj.root)),
        )
    )
    assert err is False, output
    (shared,) = [m for m in runtime.load_history(conn, sid) if isinstance(m, SharedFile)]
    assert shared.path == "legacy.csv"


def test_share_prefers_project_when_both_roots_have_the_name(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="both", root=str(tmp_path / "both"))
    Path(proj.root).mkdir(parents=True, exist_ok=True)
    (Path(proj.root) / "x.txt").write_text("project copy")
    (tmp_path / "ws" / "x.txt").write_text("workspace")
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)

    output, err = asyncio.run(
        tools.execute(
            "share_with_client", {"path": "x.txt"}, ctx=_ctx(conn, cfg, sid, Path(proj.root))
        )
    )
    assert err is False, output
    (shared,) = [m for m in runtime.load_history(conn, sid) if isinstance(m, SharedFile)]
    assert shared.path == "x.txt"
    assert shared.size == len("project copy")


def test_share_in_project_session_still_rejects_traversal(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="esc", root=str(tmp_path / "esc"))
    Path(proj.root).mkdir(parents=True, exist_ok=True)
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)

    output, err = asyncio.run(
        tools.execute(
            "share_with_client",
            {"path": "../escape.txt"},
            ctx=_ctx(conn, cfg, sid, Path(proj.root)),
        )
    )
    assert err is True
    assert "escape" in output.lower()


def test_share_in_project_session_missing_file_errors(ark_home, tmp_path):
    cfg = make_config(tmp_path)
    conn = db.init_db()
    proj = projects.create(conn, name="miss", root=str(tmp_path / "miss"))
    Path(proj.root).mkdir(parents=True, exist_ok=True)
    sid = runtime.create_session(conn, "scribe", "conversational", project_id=proj.id)

    output, err = asyncio.run(
        tools.execute(
            "share_with_client",
            {"path": "nope.png"},
            ctx=_ctx(conn, cfg, sid, Path(proj.root)),
        )
    )
    assert err is True
    assert "not a file" in output.lower()
