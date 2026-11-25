"""Call MineAI OS tools: list and invoke this session's MineAI tools through the gateway.

The MineAI tool bridge (mine-capstone#481, epic #467). One generic proxy —
zero Ark-core changes: MineAI resolves this session's ark session id
server-side to the acting user + per-session tool set + policy, so no
credential ever transits model context. Lives in the talos agent dir on
purpose: other harness agents must not inherit MineAI reach.

Gateway resolution order (mine-capstone#528): the per-session callback pair
in ``current_context().metadata["mineai_gateway"]`` (supplied by MineAI at
session create — lets one harness serve callbacks without any deploy-time
gateway config), then ``tools.mineai_gateway`` in config.json (rendered
from MINEAI_GATEWAY_URL / MINEAI_GATEWAY_SECRET at deploy time), then the
raw env vars for local runs. A metadata pair is all-or-nothing: when the
session supplies one, its url and secret are used together and NEVER mixed
with config/env halves — a session pointing the url elsewhere must not be
able to harvest the deployment's secret (confused-deputy guard).
"""

from __future__ import annotations

import json
import os
import uuid

import httpx

from ark.skills import tool
from ark.tools import current_context

_TIMEOUT = 15.0  # short: the model is waiting on this


class _GatewayNotConfigured(Exception):
    pass


def _gateway() -> "tuple[str, str]":
    ctx = current_context()
    meta = (getattr(ctx, "metadata", None) or {}).get("mineai_gateway")
    if meta is not None:
        # Pair-travel invariant: session-supplied url+secret are used
        # together or not at all — never completed from config/env.
        url = ((meta.get("url") if isinstance(meta, dict) else "") or "").rstrip("/")
        secret = (meta.get("secret") if isinstance(meta, dict) else "") or ""
        if not url or not secret:
            raise _GatewayNotConfigured
        return url, secret
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


# The @tool docstrings below are the tool descriptions the MODEL reads, in
# full (mine-capstone#1419) — keep them as instructions, and keep issue
# references in comments like this one. Narrowing: mine-capstone#692;
# idempotency: #694; compaction: #696.
@tool
def mineai_list_tools(pack: "str | None" = None, names: "list[str] | None" = None) -> str:
    """List the MineAI tools available in this session (name, description, input schema, risk class).

    Narrow the listing instead of asking for everything: `pack` returns one
    pack's tools, `names` (an array of at most 20 tool names) returns exactly
    those tools. The response also carries the pack index and a registry
    version.
    """
    session_id = current_context().session_id
    payload: "dict[str, object]" = {"session_id": session_id}
    if pack:
        payload["pack"] = pack
    if names:
        payload["names"] = list(names)[:20]
    return _post("/api/agent-gateway/list-tools", payload)


@tool
def mineai_call_tool(name: str, arguments: "dict[str, object]",
                     options: "dict[str, object] | None" = None) -> str:
    """Invoke one MineAI tool by name with a JSON object of arguments; returns {ok, result} or {ok: false, error}.

    On an error, read `error.message` and `error.remediation` (what to do
    next) and `error.retryable` before calling again — never retry blindly,
    and never repeat a call that was denied.
    A result with `replayed: true` means the call ALREADY ran (a transport
    retry was deduplicated) — do not issue it again.
    An oversized result arrives compacted, with a `_compaction` block saying
    what was dropped; pass `options` = {"full": true} only when you truly
    need the whole payload.
    A result with `agent_image.attached: false` means you were NOT shown the
    image — say so; do not describe it.
    """
    session_id = current_context().session_id
    # One idempotency key per INVOCATION, sent on both transport attempts
    # (the _post retry reuses this payload) — the gateway's replay guard
    # turns the 15 s-timeout double-send into a single execution.
    payload: "dict[str, object]" = {
        "session_id": session_id,
        "tool": name,
        "arguments": arguments,
        "idempotency_key": uuid.uuid4().hex,
    }
    if options:
        payload["options"] = options
    return _attach_agent_images(_post("/api/agent-gateway/call-tool", payload))


_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # sanity cap on a fetched tool image


def _image_blind_provider() -> str:
    """The session's provider type when its message path cannot carry tool
    result images to the model (OpenRouter / OpenAI / Google today), else "".
    An unknown provider keeps the attach behaviour."""
    try:
        from ark.provider import carries_tool_result_images

        ctx = current_context()
        provider_type = ctx.config.providers[ctx.agent.provider].provider_type
    except Exception:  # noqa: BLE001 — no provider info: assume it can
        return ""
    if not isinstance(provider_type, str) or carries_tool_result_images(provider_type):
        return ""
    return provider_type


def _attach_agent_images(body_json: str) -> str:
    """Tools that produce imagery for the MODEL (map.satellite) mark their
    result with ``agent_image`` — fetch the bytes through the gateway's
    secret-gated endpoint and embed them as the in-band ``__ark_images__``
    attachment the Ark runtime lifts into real image content blocks
    (``split_tool_images``). Any failure degrades to the text-only result
    with a note, never an error. On a provider path that cannot carry images
    the result says ``attached: false`` with the reason instead."""
    if '"agent_image"' not in body_json:
        return body_json
    try:
        body = json.loads(body_json)
    except ValueError:
        return body_json
    result = body.get("result") if isinstance(body, dict) else None
    ref = result.get("agent_image") if isinstance(result, dict) else None
    path = ref.get("url") if isinstance(ref, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        return body_json
    provider_type = _image_blind_provider()
    if provider_type:
        # Honest result (mine-capstone#1419): this provider path sends tool
        # results as text only, so the image would be dropped on the way to
        # the model. Skip the fetch and say so.
        result["agent_image"] = {
            "attached": False,
            "note": (
                f"not shown to you: the {provider_type} model path carries tool "
                "results as text only. Work from the data in this result and "
                "tell the user you could not view the image."
            ),
        }
        return json.dumps(body)
    try:
        url, secret = _gateway()
        resp = httpx.get(f"{url}{path}", headers={"X-Harness-Secret": secret}, timeout=_TIMEOUT)
        resp.raise_for_status()
        if len(resp.content) > _IMAGE_MAX_BYTES:
            raise ValueError(f"image too large ({len(resp.content)} bytes)")
        import base64

        body["__ark_images__"] = [{
            "media_type": str(ref.get("media_type") or "image/png"),
            "data_b64": base64.b64encode(resp.content).decode("ascii"),
        }]
        result["agent_image"] = {"attached": True, "media_type": ref.get("media_type") or "image/png"}
    except Exception as exc:  # noqa: BLE001 — imagery is best-effort
        result["agent_image"] = {"attached": False, "note": f"image fetch failed: {exc}"}
    return json.dumps(body)
