"""search_web + fetch_url built-in tools — end-to-end with mocked HTTP."""

from unittest.mock import MagicMock

import httpx
import pytest

from ark import tools
from ark.config import AgentConfig, Config, ProviderConfig, ServerConfig
from ark.tools import ToolContext


def make_ctx(tmp_path, *, tools_cfg=None):
    cwd = tmp_path / "ws"
    cwd.mkdir(exist_ok=True)
    agent = AgentConfig(
        name="scribe", provider="anthropic", model="m", workspace=cwd
    )
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=7777, auth_secret="x"),
        providers={"anthropic": ProviderConfig(provider_type="anthropic", api_key="k")},
        tools=tools_cfg or {},
        agents={"scribe": agent},
    )
    return ToolContext(
        conn=MagicMock(),
        config=cfg,
        agent=agent,
        session_id="s",
        cwd=cwd,
        loaded_skills=set(),
    )


@pytest.fixture
def mock_http(monkeypatch):
    holder: dict = {"handler": lambda req: httpx.Response(500, text="no handler set")}

    def handler(req: httpx.Request) -> httpx.Response:
        return holder["handler"](req)

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return holder


# ---------------------------------------------------------------------------
# search_web
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_web_calls_brave_and_renders_results(tmp_path, mock_http):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Anthropic",
                            "url": "https://anthropic.com",
                            "description": "Build with <strong>Claude</strong>.",
                        },
                        {
                            "title": "Docs",
                            "url": "https://docs.anthropic.com",
                            "description": "Reference for the Claude API.",
                        },
                    ]
                }
            },
        )

    mock_http["handler"] = handler
    ctx = make_ctx(tmp_path, tools_cfg={"brave_search": {"api_key": "BSA-fake"}})
    out, err = await tools.execute(
        "search_web", {"query": "anthropic", "count": 2}, ctx=ctx
    )
    assert err is False, out
    assert captured["url"].startswith("https://api.search.brave.com/res/v1/web/search")
    assert "q=anthropic" in captured["url"]
    assert "count=2" in captured["url"]
    assert captured["headers"].get("x-subscription-token") == "BSA-fake"
    assert "Anthropic" in out
    assert "Build with Claude" in out  # HTML tags stripped from snippet
    assert "https://docs.anthropic.com" in out


@pytest.mark.asyncio
async def test_search_web_missing_api_key(tmp_path):
    ctx = make_ctx(tmp_path, tools_cfg={})
    out, err = await tools.execute("search_web", {"query": "x"}, ctx=ctx)
    assert err is True
    assert "brave_search.api_key" in out


@pytest.mark.asyncio
async def test_search_web_http_error_propagates(tmp_path, mock_http):
    mock_http["handler"] = lambda req: httpx.Response(429, text="rate limited")
    ctx = make_ctx(tmp_path, tools_cfg={"brave_search": {"api_key": "BSA-fake"}})
    out, err = await tools.execute("search_web", {"query": "x"}, ctx=ctx)
    assert err is True
    assert "429" in out


@pytest.mark.asyncio
async def test_search_web_no_results(tmp_path, mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"web": {"results": []}})
    ctx = make_ctx(tmp_path, tools_cfg={"brave_search": {"api_key": "BSA"}})
    out, err = await tools.execute("search_web", {"query": "x"}, ctx=ctx)
    assert err is False
    assert "(no results)" in out


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_url_default_uses_httpx(tmp_path, mock_http):
    body = "<html><body>" + ("<p>real content here. " * 30) + "</body></html>"
    mock_http["handler"] = lambda req: httpx.Response(200, text=body)
    ctx = make_ctx(tmp_path)  # no resolver_sequence configured
    out, err = await tools.execute(
        "fetch_url", {"url": "https://example.com"}, ctx=ctx
    )
    assert err is False
    assert "real content here" in out


@pytest.mark.asyncio
async def test_fetch_url_falls_through_to_jina(tmp_path, mock_http):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append(url)
        if url.startswith("https://r.jina.ai/"):
            return httpx.Response(200, text="# Rendered\n\nbody from jina")
        # SPA-shell response from httpx: big raw HTML, tiny extractable text
        body = "<html><body>" + ("<script>x</script>" * 400) + "</body></html>"
        return httpx.Response(200, text=body)

    mock_http["handler"] = handler
    ctx = make_ctx(
        tmp_path,
        tools_cfg={
            "fetch_url": {
                "resolver_sequence": ["httpx", {"provider": "jina", "api_token": "tok"}]
            }
        },
    )
    out, err = await tools.execute(
        "fetch_url", {"url": "https://spa.example.com"}, ctx=ctx
    )
    assert err is False
    assert "body from jina" in out
    # We hit httpx first, then jina
    assert any("r.jina.ai" in u for u in calls)
    assert any("spa.example.com" in u and "r.jina.ai" not in u for u in calls)


@pytest.mark.asyncio
async def test_fetch_url_bad_config(tmp_path):
    ctx = make_ctx(
        tmp_path,
        tools_cfg={"fetch_url": {"resolver_sequence": ["nonexistent"]}},
    )
    out, err = await tools.execute(
        "fetch_url", {"url": "https://example.com"}, ctx=ctx
    )
    assert err is True
    assert "unknown resolver" in out


@pytest.mark.asyncio
async def test_fetch_url_all_resolvers_fail(tmp_path, mock_http):
    mock_http["handler"] = lambda req: httpx.Response(503, text="upstream")
    ctx = make_ctx(
        tmp_path,
        tools_cfg={"fetch_url": {"resolver_sequence": ["httpx", {"provider": "jina"}]}},
    )
    out, err = await tools.execute(
        "fetch_url", {"url": "https://example.com"}, ctx=ctx
    )
    assert err is True
    assert "all resolvers failed" in out
    assert "httpx" in out and "jina" in out
