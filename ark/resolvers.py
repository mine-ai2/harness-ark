"""URL fetch resolvers — pluggable backends invoked in sequence by `fetch_url`.

Each resolver attempts to retrieve and clean a URL. The chain stops at the
first resolver that returns `ok=True`. Order is determined by config:

    tools.fetch_url.resolver_sequence = [
        "httpx",
        {"provider": "jina", "api_token": "..."}
    ]

To add a new resolver: implement the `Resolver` protocol and call
`register_resolver("<name>", factory)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

import httpx

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 1024 * 1024  # 1 MB cap on returned content

# Heuristic: a 2xx response where the raw HTML is large but extracted text is
# tiny suggests an SPA shell. Fall through to a renderer-backed resolver.
_SPA_SHELL_RAW_THRESHOLD = 5000
_SPA_SHELL_TEXT_THRESHOLD = 200

_BOT_BLOCK_HINTS = (
    "cloudflare",
    "captcha",
    "please verify",
    "checking your browser",
    "ddos protection",
    "are you a robot",
)


@dataclass
class FetchResult:
    ok: bool                       # True = good enough, stop the chain
    content: str = ""              # body to show the agent (or terse error msg)
    status: int | None = None      # upstream HTTP status if applicable
    reason: str | None = None      # why ok=False, surfaced in chain diagnostics


class Resolver(Protocol):
    name: str

    async def fetch(self, url: str, *, timeout: float) -> FetchResult: ...


# ---------------------------------------------------------------------------
# httpx resolver — plain GET, HTML → markdown, no JS rendering
# ---------------------------------------------------------------------------


class HttpxResolver:
    name = "httpx"

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.max_bytes = max_bytes

    async def fetch(self, url: str, *, timeout: float) -> FetchResult:
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Ark/1.0 (+https://github.com/druths/ark)"},
            ) as client:
                r = await client.get(url)
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            return FetchResult(ok=False, reason=f"httpx {type(e).__name__}: {e}")

        body_snippet = r.text[:1000] if r.text else ""

        if 400 <= r.status_code < 500:
            # 4xx is usually a real "this page doesn't exist / you can't see it"
            # answer — falling through won't help. Exception: a 403 with a
            # bot-block fingerprint, which a renderer often gets past.
            if r.status_code == 403 and _looks_like_bot_block(body_snippet):
                return FetchResult(
                    ok=False,
                    status=r.status_code,
                    content=body_snippet,
                    reason="403 with bot-block signature",
                )
            return FetchResult(
                ok=True,
                status=r.status_code,
                content=f"HTTP {r.status_code}\n\n{body_snippet}",
            )

        if r.status_code >= 500:
            return FetchResult(
                ok=False,
                status=r.status_code,
                content=body_snippet,
                reason=f"upstream {r.status_code}",
            )

        raw = r.text or ""
        truncated = False
        if len(raw) > self.max_bytes:
            raw = raw[: self.max_bytes]
            truncated = True

        text = _html_to_text(raw)

        if (
            len(text) < _SPA_SHELL_TEXT_THRESHOLD
            and len(raw) > _SPA_SHELL_RAW_THRESHOLD
        ):
            return FetchResult(
                ok=False,
                status=r.status_code,
                content=text,
                reason="SPA shell: substantial HTML but little extracted text",
            )

        if truncated:
            text += "\n\n[truncated]"
        return FetchResult(ok=True, status=r.status_code, content=text)


# ---------------------------------------------------------------------------
# Jina Reader resolver — `https://r.jina.ai/<url>` returns rendered markdown
# ---------------------------------------------------------------------------


class JinaResolver:
    name = "jina"

    def __init__(
        self,
        api_token: str | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.api_token = api_token
        self.max_bytes = max_bytes

    async def fetch(self, url: str, *, timeout: float) -> FetchResult:
        headers = {"X-Return-Format": "markdown", "Accept": "text/plain"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        target = f"https://r.jina.ai/{url}"
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                r = await client.get(target, headers=headers)
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            return FetchResult(ok=False, reason=f"jina {type(e).__name__}: {e}")

        if r.status_code >= 400:
            return FetchResult(
                ok=False,
                status=r.status_code,
                content=r.text[:1000],
                reason=f"jina {r.status_code}",
            )

        content = r.text or ""
        truncated = False
        if len(content) > self.max_bytes:
            content = content[: self.max_bytes]
            truncated = True
        if truncated:
            content += "\n\n[truncated]"
        return FetchResult(ok=True, status=r.status_code, content=content)


# ---------------------------------------------------------------------------
# Tavily Extract resolver — POST to `https://api.tavily.com/extract`
# ---------------------------------------------------------------------------


class TavilyResolver:
    name = "tavily"

    def __init__(
        self,
        api_key: str | None = None,
        extract_depth: str = "advanced",
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.api_key = api_key
        self.extract_depth = extract_depth
        self.max_bytes = max_bytes

    async def fetch(self, url: str, *, timeout: float) -> FetchResult:
        if not self.api_key:
            return FetchResult(
                ok=False,
                reason="tavily: missing api_key (set tools.tavily.api_key or pass api_key on the resolver entry)",
            )
        body = {
            "api_key": self.api_key,
            "urls": [url],
            "extract_depth": self.extract_depth,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post("https://api.tavily.com/extract", json=body)
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            return FetchResult(ok=False, reason=f"tavily {type(e).__name__}: {e}")
        if r.status_code >= 400:
            return FetchResult(
                ok=False,
                status=r.status_code,
                content=r.text[:1000],
                reason=f"tavily {r.status_code}",
            )
        try:
            data = r.json()
        except ValueError as e:
            return FetchResult(ok=False, reason=f"tavily: invalid JSON: {e}")
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            reason = (
                failed[0].get("error", "no content") if failed else "no results returned"
            )
            return FetchResult(ok=False, reason=f"tavily: {reason}")
        content = (results[0].get("raw_content") or "").strip()
        if not content:
            return FetchResult(ok=False, reason="tavily: empty raw_content")
        truncated = False
        if len(content) > self.max_bytes:
            content = content[: self.max_bytes]
            truncated = True
        if truncated:
            content += "\n\n[truncated]"
        return FetchResult(ok=True, status=r.status_code, content=content)


# ---------------------------------------------------------------------------
# Registry + chain driver
# ---------------------------------------------------------------------------


ResolverFactory = Callable[[dict[str, Any]], Resolver]


class ResolverConfigError(Exception):
    pass


RESOLVERS: dict[str, ResolverFactory] = {
    "httpx": lambda cfg: HttpxResolver(max_bytes=int(cfg.get("max_bytes", DEFAULT_MAX_BYTES))),
    "jina": lambda cfg: JinaResolver(
        api_token=cfg.get("api_token"),
        max_bytes=int(cfg.get("max_bytes", DEFAULT_MAX_BYTES)),
    ),
    "tavily": lambda cfg: TavilyResolver(
        api_key=cfg.get("api_key"),
        extract_depth=cfg.get("extract_depth", "advanced"),
        max_bytes=int(cfg.get("max_bytes", DEFAULT_MAX_BYTES)),
    ),
}


def register_resolver(name: str, factory: ResolverFactory) -> None:
    RESOLVERS[name] = factory


def build_chain(
    spec: list | None, *, vendor_blocks: dict[str, dict[str, Any]] | None = None
) -> list[Resolver]:
    """Parse a resolver_sequence config list into a list of Resolver instances.

    Each entry may be a bare provider name (string) or an object with
    `{provider: <name>, ...provider-specific config...}`.

    `vendor_blocks` is the full `tools` config dict — when an entry's provider
    name matches a top-level vendor block (e.g. `tools.tavily`), fields from
    that block are used as a fallback for any keys not present in the entry.
    This lets you type `"tools.tavily.api_key"` once and have both `search_web`
    (the search tool) and the `tavily` resolver pick it up.

    Unknown providers raise `ResolverConfigError`. An empty/missing spec
    defaults to a single httpx resolver, matching v1's no-config behavior.
    """

    if not spec:
        return [HttpxResolver()]
    vendor_blocks = vendor_blocks or {}
    chain: list[Resolver] = []
    for entry in spec:
        if isinstance(entry, str):
            name, cfg = entry, {}
        elif isinstance(entry, dict):
            name = entry.get("provider")
            if not name or not isinstance(name, str):
                raise ResolverConfigError(
                    f"resolver entry missing string 'provider': {entry!r}"
                )
            cfg = {k: v for k, v in entry.items() if k != "provider"}
        else:
            raise ResolverConfigError(
                f"resolver entry must be string or object: {entry!r}"
            )
        factory = RESOLVERS.get(name)
        if factory is None:
            raise ResolverConfigError(
                f"unknown resolver {name!r}. Known: {sorted(RESOLVERS)}"
            )
        # Merge vendor block as fallback (entry config wins on conflict).
        vendor = vendor_blocks.get(name) or {}
        merged_cfg = {**vendor, **cfg}
        chain.append(factory(merged_cfg))
    return chain


async def fetch_with_chain(
    url: str, chain: list[Resolver], timeout: float = DEFAULT_TIMEOUT
) -> FetchResult:
    """Run the chain until one returns ok=True. On all-failed, return the last
    result with a chain summary appended so the agent sees what was tried."""

    attempts: list[str] = []
    last: FetchResult | None = None
    for resolver in chain:
        result = await resolver.fetch(url, timeout=timeout)
        last = result
        if result.ok:
            return result
        attempts.append(f"{resolver.name}: {result.reason or 'unknown'}")
    summary = "; ".join(attempts)
    content = (last.content if last else "") + f"\n\n[all resolvers failed: {summary}]"
    return FetchResult(
        ok=False,
        content=content,
        status=last.status if last else None,
        reason=summary,
    )


# ---------------------------------------------------------------------------
# HTML → markdown helper
# ---------------------------------------------------------------------------


def _html_to_text(html: str) -> str:
    import html2text

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0  # don't hard-wrap
    return h.handle(html).strip()


def _looks_like_bot_block(body: str) -> bool:
    low = body.lower()
    return any(p in low for p in _BOT_BLOCK_HINTS)
