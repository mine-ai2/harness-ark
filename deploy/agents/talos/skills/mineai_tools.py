"""Call MineAI OS tools: list and invoke this session's MineAI tools through the gateway.

The MineAI tool bridge (mine-capstone#481, epic #467). One generic proxy —
zero Ark-core changes: MineAI resolves this session's ark session id
server-side to the acting user + per-session tool set + policy, so no
credential ever transits model context. Lives in the talos agent dir on
purpose: other harness agents must not inherit MineAI reach.

Gateway config comes from ``tools.mineai_gateway`` in config.json (rendered
from MINEAI_GATEWAY_URL / MINEAI_GATEWAY_SECRET at deploy time), with the
raw env vars as fallback for local runs.
"""

from __future__ import annotations

import json
import os

import httpx

from ark.skills import tool
from ark.tools import current_context

_TIMEOUT = 15.0  # short: the model is waiting on this


class _GatewayNotConfigured(Exception):
    pass


def _gateway() -> "tuple[str, str]":
    ctx = current_context()
    cfg = (getattr(ctx.config, "tools", None) or {}).get("mineai_gateway") or {}
    url = (cfg.get("url") or os.environ.get("MINEAI_GATEWAY_URL", "")).rstrip("/")
    secret = cfg.get("secret") or os.environ.get("MINEAI_GATEWAY_SECRET", "")
    if not url or not secret:
        raise _GatewayNotConfigured
    return url, secret


def _post(path: str, payload: dict) -> str:
    """POST to the gateway; structured ``{ok, ...}`` passthrough either way.

    Retries once on transport failure (the gateway is idempotent for reads
    and audited for everything); HTTP error bodies are passed through so the
    model can read MineAI's structured denials and correct itself.
    """
    try:
        url, secret = _gateway()
    except _GatewayNotConfigured:
        return json.dumps(
            {"ok": False, "error": {"code": "gateway_not_configured",
                                    "message": "MineAI gateway is not configured on this harness"}}
        )
    headers = {"X-Harness-Secret": secret}
    last_error = None
    for _attempt in range(2):
        try:
            resp = httpx.post(f"{url}{path}", json=payload, headers=headers, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        try:
            body = resp.json()
        except ValueError:
            return json.dumps(
                {"ok": False, "error": {"code": "gateway_error",
                                        "message": f"non-JSON gateway response (HTTP {resp.status_code})"}}
            )
        if resp.status_code >= 400:
            # FastAPI wraps our structured error in {"detail": ...} — unwrap
            # so the model always sees the same {ok:false, error} shape.
            detail = body.get("detail")
            if isinstance(detail, dict) and "error" in detail:
                return json.dumps(detail)
            return json.dumps(
                {"ok": False, "error": {"code": f"http_{resp.status_code}",
                                        "message": str(detail or body)}}
            )
        return json.dumps(body)
    return json.dumps(
        {"ok": False, "error": {"code": "gateway_unreachable",
                                "message": f"gateway request failed twice: {last_error}"}}
    )


@tool
def mineai_list_tools() -> str:
    """List the MineAI tools available in this session (name, description, input schema, risk class)."""
    session_id = current_context().session_id
    return _post("/api/agent-gateway/list-tools", {"session_id": session_id})


@tool
def mineai_call_tool(name: str, arguments: "dict[str, object]") -> str:
    """Invoke one MineAI tool by name with a JSON-object of arguments; returns {ok, result} or {ok: false, error}."""
    session_id = current_context().session_id
    return _post(
        "/api/agent-gateway/call-tool",
        {"session_id": session_id, "tool": name, "arguments": arguments},
    )
