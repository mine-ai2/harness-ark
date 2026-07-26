import asyncio
from unittest.mock import MagicMock

from ark import tools
from ark.config import AgentConfig
from ark.tools import ToolContext


def make_ctx(tmp_path, agent_name="scribe"):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent = AgentConfig(
        name=agent_name,
        provider="anthropic",
        model="claude-opus-4-7",
        workspace=cwd,
    )
    return ToolContext(
        conn=MagicMock(),
        config=MagicMock(),
        agent=agent,
        session_id="sess-1",
        cwd=cwd,
        loaded_skills=set(),
    )


def run(name, args, ctx):
    return asyncio.run(tools.execute(name, args, ctx=ctx))


def test_read_and_write_file(tmp_path):
    ctx = make_ctx(tmp_path)
    p = tmp_path / "f.txt"
    output, err = run("write_file", {"path": str(p), "content": "hello"}, ctx)
    assert err is False
    assert p.read_text() == "hello"
    output, err = run("read_file", {"path": str(p)}, ctx)
    assert err is False
    assert output == "hello"


def test_write_file_refuses_other_agent_workspace(tmp_path):
    """An agent shouldn't be able to scribble into a peer agent's workspace.
    read_file / run_command are intentionally NOT restricted this way — the
    concern here is preventing accidental / adversarial cross-writes."""
    from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig

    my_ws = tmp_path / "mine"
    my_ws.mkdir()
    peer_ws = tmp_path / "peer"
    peer_ws.mkdir()

    me = AgentConfig(name="me", provider="a", model="m", workspace=my_ws)
    peer = AgentConfig(name="peer", provider="a", model="m", workspace=peer_ws)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={"me": me, "peer": peer},
    )
    ctx = ToolContext(
        conn=MagicMock(), config=cfg, agent=me, session_id="s",
        cwd=my_ws, loaded_skills=set(),
    )

    # Own workspace: fine
    own_target = my_ws / "note.md"
    output, err = run("write_file", {"path": str(own_target), "content": "mine"}, ctx)
    assert err is False
    assert own_target.read_text() == "mine"

    # Peer's workspace: refused
    peer_target = peer_ws / "note.md"
    output, err = run(
        "write_file", {"path": str(peer_target), "content": "sneaky"}, ctx
    )
    assert err is True
    assert "another agent's workspace" in output
    assert "peer" in output
    assert not peer_target.exists()

    # Peer's nested subdir: also refused
    output, err = run(
        "write_file",
        {"path": str(peer_ws / "sub" / "deep.md"), "content": "sneakier"},
        ctx,
    )
    assert err is True
    assert not (peer_ws / "sub").exists()


def test_read_missing_file_errors(tmp_path):
    ctx = make_ctx(tmp_path)
    output, err = run("read_file", {"path": str(tmp_path / "nope")}, ctx)
    assert err is True
    assert "No such file" in output or "not found" in output.lower()


def test_list_files(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "sub").mkdir()
    output, err = run("list_files", {"path": str(tmp_path)}, ctx)
    assert err is False
    assert "a.py" in output and "b.txt" in output and "sub/" in output

    output, err = run("list_files", {"path": str(tmp_path), "pattern": "*.py"}, ctx)
    assert err is False
    assert "a.py" in output and "b.txt" not in output


def test_run_command(tmp_path):
    ctx = make_ctx(tmp_path)
    output, err = run("run_command", {"command": "echo hello"}, ctx)
    assert err is False
    assert "hello" in output
    assert "exit code: 0" in output


def test_run_command_timeout(tmp_path):
    ctx = make_ctx(tmp_path)
    output, err = run(
        "run_command", {"command": "sleep 2", "timeout_seconds": 0.1}, ctx
    )
    assert err is True
    assert "timed out" in output


def test_unknown_tool(tmp_path):
    ctx = make_ctx(tmp_path)
    output, err = run("nope", {}, ctx)
    assert err is True
    assert "unknown" in output


def test_invalid_args(tmp_path):
    ctx = make_ctx(tmp_path)
    # read_file requires a path; passing nothing should produce an error result, not raise
    output, err = run("read_file", {}, ctx)
    assert err is True
