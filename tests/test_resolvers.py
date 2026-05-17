"""Resolver chain: HTTP heuristics + chain composition + driver behavior."""

import asyncio

import httpx
import pytest

from ark import resolvers
from ark.resolvers import (
    FetchResult,
    HttpxResolver,
    JinaResolver,
    Resolver,
    ResolverConfigError,
    build_chain,
    fetch_with_chain,
)


def mock_client(handler):
    """Wrap an httpx MockTransport handler in an AsyncClient factory.

    The resolver uses `async with httpx.AsyncClient(...)` directly, so we
    monkeypatch AsyncClient to return one with our transport baked in.
    """
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        real_init(self, *args, **kwargs)

    return patched_init, real_init


@pytest.fixture
def mock_http(monkeypatch):
    """Routes all httpx.AsyncClient traffic through a handler set per-test."""

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
# build_chain
# ---------------------------------------------------------------------------


def test_build_chain_default():
    chain = build_chain(None)
    assert len(chain) == 1
    assert chain[0].name == "httpx"


def test_build_chain_string_shorthand():
    chain = build_chain(["httpx"])
    assert len(chain) == 1
    assert isinstance(chain[0], HttpxResolver)


def test_build_chain_dict_with_config():
    chain = build_chain([{"provider": "jina", "api_token": "tok-abc"}])
    assert len(chain) == 1
    assert isinstance(chain[0], JinaResolver)
    assert chain[0].api_token == "tok-abc"


def test_build_chain_mixed():
    chain = build_chain(["httpx", {"provider": "jina"}])
    assert [r.name for r in chain] == ["httpx", "jina"]


def test_build_chain_rejects_unknown():
    with pytest.raises(ResolverConfigError, match="unknown resolver"):
        build_chain(["does-not-exist"])


def test_build_chain_rejects_missing_provider():
    with pytest.raises(ResolverConfigError, match="missing"):
        build_chain([{"foo": "bar"}])


def test_build_chain_rejects_bad_entry():
    with pytest.raises(ResolverConfigError, match="must be string or object"):
        build_chain([42])


# ---------------------------------------------------------------------------
# HttpxResolver heuristics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_httpx_200_with_content(mock_http):
    body = "<html><body>" + ("<p>plenty of words here. " * 50) + "</body></html>"
    mock_http["handler"] = lambda req: httpx.Response(200, text=body)
    r = await HttpxResolver().fetch("https://example.com", timeout=5)
    assert r.ok is True
    assert "plenty of words" in r.content


@pytest.mark.asyncio
async def test_httpx_spa_shell_falls_through(mock_http):
    body = "<html><head></head><body>" + ("<script>" + "x" * 200 + "</script>") * 30 + "</body></html>"
    mock_http["handler"] = lambda req: httpx.Response(200, text=body)
    r = await HttpxResolver().fetch("https://example.com", timeout=5)
    assert r.ok is False
    assert "SPA shell" in r.reason


@pytest.mark.asyncio
async def test_httpx_404_stops_chain(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(404, text="Not Found")
    r = await HttpxResolver().fetch("https://example.com/nope", timeout=5)
    assert r.ok is True  # don't fall through — 404 is a real answer
    assert "404" in r.content


@pytest.mark.asyncio
async def test_httpx_403_botblock_falls_through(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(
        403, text="<html>Cloudflare: checking your browser…</html>"
    )
    r = await HttpxResolver().fetch("https://example.com", timeout=5)
    assert r.ok is False
    assert "bot-block" in r.reason


@pytest.mark.asyncio
async def test_httpx_403_normal_stays(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(403, text="Forbidden: not your account")
    r = await HttpxResolver().fetch("https://example.com", timeout=5)
    assert r.ok is True  # plain 403 is a real answer; don't escalate


@pytest.mark.asyncio
async def test_httpx_5xx_falls_through(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(503, text="upstream gone")
    r = await HttpxResolver().fetch("https://example.com", timeout=5)
    assert r.ok is False
    assert "503" in r.reason


@pytest.mark.asyncio
async def test_httpx_timeout_falls_through(mock_http):
    def boom(req):
        raise httpx.ConnectTimeout("nope")

    mock_http["handler"] = boom
    r = await HttpxResolver().fetch("https://example.com", timeout=5)
    assert r.ok is False
    assert "ConnectTimeout" in r.reason


@pytest.mark.asyncio
async def test_httpx_truncates_at_max_bytes(mock_http):
    body = "<html><body>" + "a" * 10_000 + "</body></html>"
    mock_http["handler"] = lambda req: httpx.Response(200, text=body)
    r = await HttpxResolver(max_bytes=500).fetch("https://example.com", timeout=5)
    assert r.ok is True
    assert "[truncated]" in r.content


# ---------------------------------------------------------------------------
# JinaResolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jina_composes_url_and_returns_markdown(mock_http):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        return httpx.Response(200, text="# Rendered\n\nbody from jina")

    mock_http["handler"] = handler
    r = await JinaResolver().fetch("https://example.com/article", timeout=5)
    assert r.ok is True
    assert "body from jina" in r.content
    assert captured["url"] == "https://r.jina.ai/https://example.com/article"
    assert "authorization" not in {k.lower() for k in captured["headers"]}
    assert captured["headers"].get("x-return-format") == "markdown"


@pytest.mark.asyncio
async def test_jina_sends_auth_when_token_present(mock_http):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        return httpx.Response(200, text="ok")

    mock_http["handler"] = handler
    await JinaResolver(api_token="tok-secret").fetch("https://example.com", timeout=5)
    assert captured["headers"].get("authorization") == "Bearer tok-secret"


@pytest.mark.asyncio
async def test_jina_429_fails(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(429, text="slow down")
    r = await JinaResolver().fetch("https://example.com", timeout=5)
    assert r.ok is False
    assert "429" in r.reason


# ---------------------------------------------------------------------------
# Chain driver
# ---------------------------------------------------------------------------


class _StubResolver:
    def __init__(self, name: str, result: FetchResult):
        self.name = name
        self.result = result
        self.calls = 0

    async def fetch(self, url: str, *, timeout: float) -> FetchResult:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_chain_stops_at_first_ok():
    a = _StubResolver("a", FetchResult(ok=True, content="from a"))
    b = _StubResolver("b", FetchResult(ok=True, content="from b"))
    result = await fetch_with_chain("u", [a, b])
    assert result.content == "from a"
    assert a.calls == 1 and b.calls == 0


@pytest.mark.asyncio
async def test_chain_falls_through_until_ok():
    a = _StubResolver("a", FetchResult(ok=False, reason="nope"))
    b = _StubResolver("b", FetchResult(ok=True, content="from b"))
    result = await fetch_with_chain("u", [a, b])
    assert result.content == "from b"
    assert a.calls == 1 and b.calls == 1


@pytest.mark.asyncio
async def test_chain_all_fail_summarizes():
    a = _StubResolver("a", FetchResult(ok=False, reason="timeout"))
    b = _StubResolver("b", FetchResult(ok=False, reason="rate-limit", content="last body"))
    result = await fetch_with_chain("u", [a, b])
    assert result.ok is False
    assert "a: timeout" in result.reason
    assert "b: rate-limit" in result.reason
    assert "all resolvers failed" in result.content
    assert "last body" in result.content
