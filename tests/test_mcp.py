"""MCP server integration — config parsing, schema translation, dispatch,
and the skills/mcp unification."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from unittest.mock import MagicMock

import pytest

from ark import config, mcp
from ark.config import AgentConfig, Config, MCPServerConfig, ProviderConfig, ServerConfig
from ark.tools import ToolContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_mcp_singleton():
    """Every test starts with a clean manager."""
    mcp.reset_for_tests()
    yield
    mcp.reset_for_tests()


def write_config(ark_home, data):
    (ark_home / "config.json").write_text(json.dumps(data))


def minimal_config():
    return {
        "server": {"auth_secret": "shh"},
        "providers": {"anthropic": {"provider_type": "anthropic", "api_key": "k"}},
        "agents": {"scribe": {"provider": "anthropic", "model": "claude-opus-4-7"}},
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_no_mcp_servers_is_fine(ark_home):
    """Configs without an mcp_servers block still parse — additive change."""
    write_config(ark_home, minimal_config())
    cfg = config.load()
    assert cfg.mcp_servers == {}
    assert cfg.agents["scribe"].mcp_servers == []
    assert cfg.agents["scribe"].always_loaded_mcp_servers == []


def test_stdio_server_config(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {
        "linear": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-linear"],
            "env": {"LINEAR_API_KEY": "xxx"},
        }
    }
    write_config(ark_home, data)
    cfg = config.load()
    srv = cfg.mcp_servers["linear"]
    assert srv.transport == "stdio"
    assert srv.command == "npx"
    assert srv.args == ["-y", "@modelcontextprotocol/server-linear"]
    assert srv.env == {"LINEAR_API_KEY": "xxx"}
    assert srv.timeout_seconds == 30.0  # default


def test_http_server_config(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {
        "notion": {
            "transport": "http",
            "url": "https://mcp.notion.com/mcp",
            "headers": {"Authorization": "Bearer nti_..."},
            "timeout_seconds": 60,
        }
    }
    write_config(ark_home, data)
    cfg = config.load()
    srv = cfg.mcp_servers["notion"]
    assert srv.transport == "http"
    assert srv.url == "https://mcp.notion.com/mcp"
    assert srv.headers == {"Authorization": "Bearer nti_..."}
    assert srv.timeout_seconds == 60.0


def test_transport_required(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {"bad": {"command": "x"}}
    write_config(ark_home, data)
    with pytest.raises(config.ConfigError, match="transport must be"):
        config.load()


def test_stdio_requires_command(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {"bad": {"transport": "stdio"}}
    write_config(ark_home, data)
    with pytest.raises(config.ConfigError, match="command is required"):
        config.load()


def test_http_requires_url(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {"bad": {"transport": "http"}}
    write_config(ark_home, data)
    with pytest.raises(config.ConfigError, match="url is required"):
        config.load()


def test_agent_mcp_servers_must_reference_configured(ark_home):
    data = minimal_config()
    data["agents"]["scribe"]["mcp_servers"] = ["notion"]  # not in mcp_servers
    write_config(ark_home, data)
    with pytest.raises(config.ConfigError, match="unknown server 'notion'"):
        config.load()


def test_always_loaded_must_be_in_agent_mcp_servers(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {
        "linear": {"transport": "stdio", "command": "npx", "args": []}
    }
    # Agent has NO mcp_servers but tries to always-load — bad.
    data["agents"]["scribe"]["always_loaded_mcp_servers"] = ["linear"]
    write_config(ark_home, data)
    with pytest.raises(config.ConfigError, match="not in agents.scribe.mcp_servers"):
        config.load()


def test_agent_mcp_servers_full_parsing(ark_home):
    data = minimal_config()
    data["mcp_servers"] = {
        "linear": {"transport": "stdio", "command": "npx", "args": []},
        "notion": {"transport": "http", "url": "http://x"},
    }
    data["agents"]["scribe"]["mcp_servers"] = ["linear", "notion"]
    data["agents"]["scribe"]["always_loaded_mcp_servers"] = ["linear"]
    write_config(ark_home, data)
    cfg = config.load()
    assert cfg.agents["scribe"].mcp_servers == ["linear", "notion"]
    assert cfg.agents["scribe"].always_loaded_mcp_servers == ["linear"]


# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------


def test_translate_tools_prefixes_name_and_preserves_schema():
    raw = [
        {
            "name": "create_issue",
            "description": "Create a Linear issue.",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
        {
            "name": "list_issues",
            "description": "List issues.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    schemas, mapping = mcp._translate_tools("linear", raw)
    names = [s.name for s in schemas]
    assert names == ["linear__create_issue", "linear__list_issues"]
    assert mapping["linear__create_issue"] == "create_issue"
    assert schemas[0].description == "Create a Linear issue."
    assert schemas[0].input_schema["required"] == ["title"]


def test_translate_tools_defaults_missing_input_schema():
    raw = [{"name": "ping"}]
    schemas, _ = mcp._translate_tools("srv", raw)
    assert schemas[0].input_schema == {"type": "object", "properties": {}}


def test_translate_tools_skips_unnamed():
    raw = [{"description": "no name"}, {"name": "ok"}]
    schemas, mapping = mcp._translate_tools("srv", raw)
    assert [s.name for s in schemas] == ["srv__ok"]


# ---------------------------------------------------------------------------
# Response flattening
# ---------------------------------------------------------------------------


def test_extract_text_joins_text_content():
    result = MagicMock()
    result.isError = False
    result.content = [
        MagicMock(type="text", text="first"),
        MagicMock(type="text", text="second"),
    ]
    assert mcp._extract_text(result) == "first\nsecond"


def test_extract_text_flags_error():
    result = MagicMock()
    result.isError = True
    result.content = [MagicMock(type="text", text="something broke")]
    assert mcp._extract_text(result).startswith("MCP tool error:")


def test_extract_text_placeholder_for_non_text():
    result = MagicMock()
    result.isError = False
    result.content = [MagicMock(type="image", data=b"...")]
    assert "image content omitted" in mcp._extract_text(result)


def test_extract_text_handles_dict_shape():
    result = {"isError": False, "content": [{"type": "text", "text": "hi"}]}
    assert mcp._extract_text(result) == "hi"


def test_extract_text_empty():
    result = MagicMock()
    result.isError = False
    result.content = []
    assert mcp._extract_text(result) == "(empty response)"


# ---------------------------------------------------------------------------
# Manager start + call, using a stub connection factory
# ---------------------------------------------------------------------------


class StubConnection:
    """Records calls so tests can assert on them."""

    def __init__(self, tools, responses):
        self.tools = tools
        self.responses = responses  # name → return value (or Exception to raise)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        resp = self.responses.get(name)
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_factory(tools, responses):
    async def factory(cfg):
        stack = AsyncExitStack()
        connection = StubConnection(tools, responses)
        return connection, stack, f"stub {cfg.name}", tools
    return factory


def make_stub_response(text, is_error=False):
    r = MagicMock()
    r.isError = is_error
    r.content = [MagicMock(type="text", text=text)]
    return r


@pytest.mark.asyncio
async def test_manager_start_lists_tools_and_caches_schemas():
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "Search things.", "inputSchema": {}}],
            responses={},
        )
    )
    await manager.start({
        "linear": MCPServerConfig(name="linear", transport="stdio", command="x")
    })
    srv = manager.get("linear")
    assert srv is not None
    assert srv.error is None
    assert srv.description == "stub linear"
    assert [t.name for t in srv.tools] == ["linear__search"]
    assert srv.raw_tool_names == {"linear__search": "search"}


@pytest.mark.asyncio
async def test_manager_call_tool_dispatches_and_returns_text():
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={"search": make_stub_response("found stuff")},
        )
    )
    await manager.start({
        "linear": MCPServerConfig(name="linear", transport="stdio", command="x")
    })
    output = await manager.call_tool("linear", "search", {"query": "bugs"})
    assert output == "found stuff"
    srv = manager.get("linear")
    assert srv.connection.calls == [("search", {"query": "bugs"})]


@pytest.mark.asyncio
async def test_manager_records_startup_failure_without_crashing():
    async def failing_factory(cfg):
        raise RuntimeError("subprocess dead")

    manager = mcp.get_manager()
    manager.set_connection_factory(failing_factory)
    await manager.start({
        "flaky": MCPServerConfig(name="flaky", transport="stdio", command="x")
    })
    srv = manager.get("flaky")
    assert srv is not None
    assert srv.error is not None
    assert "subprocess dead" in srv.error


@pytest.mark.asyncio
async def test_manager_call_tool_on_broken_server_errors_cleanly():
    async def failing_factory(cfg):
        raise RuntimeError("nope")

    manager = mcp.get_manager()
    manager.set_connection_factory(failing_factory)
    await manager.start({
        "flaky": MCPServerConfig(name="flaky", transport="stdio", command="x")
    })
    with pytest.raises(mcp.MCPError, match="unavailable"):
        await manager.call_tool("flaky", "whatever", {})


@pytest.mark.asyncio
async def test_manager_call_tool_wraps_provider_errors():
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "flaky", "description": "", "inputSchema": {}}],
            responses={"flaky": RuntimeError("upstream 500")},
        )
    )
    await manager.start({
        "srv": MCPServerConfig(name="srv", transport="stdio", command="x")
    })
    with pytest.raises(mcp.MCPError, match="upstream 500"):
        await manager.call_tool("srv", "flaky", {})


# ---------------------------------------------------------------------------
# End-to-end: an agent's active_schemas + execute route MCP tools
# ---------------------------------------------------------------------------


def _make_ctx(ark_home, agent, cfg, loaded_skills=None):
    return ToolContext(
        conn=MagicMock(),
        config=cfg,
        agent=agent,
        session_id="s",
        cwd=ark_home,
        loaded_skills=loaded_skills or set(),
    )


def _make_full_config(mcp_servers):
    return Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"a": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools={},
        agents={},
        mcp_servers=mcp_servers,
    )


@pytest.mark.asyncio
async def test_always_loaded_mcp_server_appears_in_active_schemas(ark_home):
    from ark import tools

    linear_cfg = MCPServerConfig(name="linear", transport="stdio", command="x")
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={},
        )
    )
    await manager.start({"linear": linear_cfg})

    agent = AgentConfig(
        name="me",
        provider="a",
        model="m",
        workspace=ark_home,
        mcp_servers=["linear"],
        always_loaded_mcp_servers=["linear"],
    )
    schemas = tools.active_schemas(agent, set())
    names = [s.name for s in schemas]
    assert "linear__search" in names


@pytest.mark.asyncio
async def test_non_always_loaded_mcp_server_hidden_until_load(ark_home):
    from ark import tools

    linear_cfg = MCPServerConfig(name="linear", transport="stdio", command="x")
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={},
        )
    )
    await manager.start({"linear": linear_cfg})

    agent = AgentConfig(
        name="me",
        provider="a",
        model="m",
        workspace=ark_home,
        mcp_servers=["linear"],  # available, not always-loaded
    )
    # Not loaded: hidden.
    names = [s.name for s in tools.active_schemas(agent, set())]
    assert "linear__search" not in names
    # After load_skill-style activation: visible.
    names = [s.name for s in tools.active_schemas(agent, {"linear"})]
    assert "linear__search" in names


@pytest.mark.asyncio
async def test_execute_routes_to_mcp_and_returns_text(ark_home):
    from ark import tools

    linear_cfg = MCPServerConfig(name="linear", transport="stdio", command="x")
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={"search": make_stub_response("issue #42")},
        )
    )
    await manager.start({"linear": linear_cfg})

    agent = AgentConfig(
        name="me",
        provider="a",
        model="m",
        workspace=ark_home,
        mcp_servers=["linear"],
        always_loaded_mcp_servers=["linear"],
    )
    ctx = _make_ctx(ark_home, agent, _make_full_config({"linear": linear_cfg}))
    output, err = await tools.execute("linear__search", {"query": "bugs"}, ctx=ctx)
    assert err is False
    assert output == "issue #42"


@pytest.mark.asyncio
async def test_execute_refuses_mcp_tool_from_unauthorized_server(ark_home):
    """An agent that doesn't list 'linear' in its mcp_servers can't invoke
    linear__anything even though the server is globally configured."""
    from ark import tools

    linear_cfg = MCPServerConfig(name="linear", transport="stdio", command="x")
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={"search": make_stub_response("secret")},
        )
    )
    await manager.start({"linear": linear_cfg})

    agent = AgentConfig(
        name="stranger", provider="a", model="m", workspace=ark_home,
        mcp_servers=[],  # not authorized
    )
    ctx = _make_ctx(ark_home, agent, _make_full_config({"linear": linear_cfg}))
    output, err = await tools.execute("linear__search", {}, ctx=ctx)
    assert err is True
    assert "unknown tool" in output


@pytest.mark.asyncio
async def test_load_skill_can_load_an_mcp_server(ark_home):
    """The skills/mcp unification: load_skill accepts an MCP server name."""
    from ark import tools

    linear_cfg = MCPServerConfig(name="linear", transport="stdio", command="x")
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={},
        )
    )
    await manager.start({"linear": linear_cfg})

    agent = AgentConfig(
        name="me", provider="a", model="m", workspace=ark_home,
        mcp_servers=["linear"],
    )
    ctx = _make_ctx(ark_home, agent, _make_full_config({"linear": linear_cfg}))
    output, err = await tools.execute("load_skill", {"name": "linear"}, ctx=ctx)
    assert err is False, output
    assert "MCP server 'linear'" in output
    assert "linear__search" in output
    # Side effect: schemas now include the MCP tools.
    names = [s.name for s in tools.active_schemas(agent, ctx.loaded_skills)]
    assert "linear__search" in names


@pytest.mark.asyncio
async def test_list_skills_surfaces_mcp_servers(ark_home):
    from ark import tools

    linear_cfg = MCPServerConfig(name="linear", transport="stdio", command="x")
    manager = mcp.get_manager()
    manager.set_connection_factory(
        make_factory(
            tools=[{"name": "search", "description": "", "inputSchema": {}}],
            responses={},
        )
    )
    await manager.start({"linear": linear_cfg})

    agent = AgentConfig(
        name="me", provider="a", model="m", workspace=ark_home,
        mcp_servers=["linear"],
        always_loaded_mcp_servers=["linear"],
    )
    ctx = _make_ctx(ark_home, agent, _make_full_config({"linear": linear_cfg}))
    output, err = await tools.execute("list_skills", {}, ctx=ctx)
    assert err is False
    assert "linear (mcp)" in output
    assert "[loaded]" in output  # always-loaded → marked
