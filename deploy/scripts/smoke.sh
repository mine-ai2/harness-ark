#!/usr/bin/env bash
# External smoke test for a harness environment (mine-capstone#469).
#
# Runs from OUTSIDE the VM (operator laptop or CI runner) against the
# public domain, so it exercises DNS, the DO firewall, Caddy TLS, and the
# WebSocket proxy path end to end. Also run post-deploy by
# .github/workflows/talos-deploy.yml.
#
# Checks:
#   1. https://<domain>/health returns {"ok": true} with a valid cert.
#   2. wss://<domain>/events rejects an unauthenticated connect.
#   3. wss://<domain>/events accepts the bearer token and answers an
#      application-level round-trip (the server sends nothing on connect,
#      so we send {"type":"ping"} and expect the documented
#      "unsupported command type" error frame back).
#   4. With --soak N: holds the authenticated socket open N seconds, then
#      repeats the round-trip (acceptance: --soak 330 for the >5 min
#      criterion; not run in CI).
#
# Usage: ARK_AUTH_SECRET=... smoke.sh <domain> [--soak <seconds>]
# The secret is taken from the environment only — never pass it as an
# argument (argv is visible in `ps`).
set -Eeuo pipefail

die() {
    echo "smoke.sh: error: $*" >&2
    exit 1
}

ok() {
    echo "smoke.sh: ok: $*"
}

DOMAIN="${1:-}"
[[ -n $DOMAIN && $DOMAIN != --* ]] || die "usage: ARK_AUTH_SECRET=... smoke.sh <domain> [--soak <seconds>]"
SOAK_SECONDS=0
if [[ ${2:-} == --soak ]]; then
    SOAK_SECONDS="${3:-}"
    [[ $SOAK_SECONDS =~ ^[0-9]+$ ]] || die "--soak requires a number of seconds"
fi

[[ -n ${ARK_AUTH_SECRET:-} ]] || die "ARK_AUTH_SECRET is not set"
command -v curl >/dev/null 2>&1 || die "curl not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c 'import websockets' 2>/dev/null \
    || die "python websockets library missing — pip install 'websockets>=13.0'"

# --- 1. Health over TLS (full cert validation — no -k) -----------------------

body="$(curl -fsS --max-time 10 "https://$DOMAIN/health")" \
    || die "GET https://$DOMAIN/health failed"
python3 - "$body" <<'PY' || die "/health did not return {\"ok\": true}"
import json, sys
assert json.loads(sys.argv[1]) == {"ok": True}
PY
ok "https://$DOMAIN/health returns {\"ok\": true} with valid TLS"

# --- 2 + 3 (+ soak). WebSocket checks ----------------------------------------

# Secret and parameters travel to python via the environment, not argv.
SMOKE_DOMAIN="$DOMAIN" SMOKE_SOAK="$SOAK_SECONDS" python3 <<'PY' || exit 1
import asyncio
import json
import os
import sys

import websockets

DOMAIN = os.environ["SMOKE_DOMAIN"]
SECRET = os.environ["ARK_AUTH_SECRET"]
SOAK = int(os.environ["SMOKE_SOAK"])
URL = f"wss://{DOMAIN}/events"


def ok(msg):
    print(f"smoke.sh: ok: {msg}")


def fail(msg):
    print(f"smoke.sh: error: {msg}", file=sys.stderr)
    sys.exit(1)


async def roundtrip(ws, label):
    # The server sends nothing on connect; the documented reply to an
    # unsupported command type is the reliable liveness probe (never send
    # user_message here — it would start a real agent turn). The socket
    # also broadcasts every session's events, so on a live system the
    # reply may be interleaved with unrelated frames: skip until we see
    # our error frame or the deadline passes.
    await ws.send(json.dumps({"type": "ping"}))
    deadline = asyncio.get_event_loop().time() + 10
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            fail(f"{label}: no reply to probe within 10s")
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if reply.get("type") == "error" and "unsupported command type" in reply.get("message", ""):
            break
    ok(label)


async def main():
    # Unauthenticated connect must be rejected (HTTP 403 from the ASGI
    # translation of close-before-accept, or WS close code 1008).
    try:
        async with websockets.connect(URL, open_timeout=10) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
        fail("unauthenticated /events connect was NOT rejected")
    except websockets.exceptions.InvalidStatus as e:
        code = e.response.status_code
        if code not in (401, 403):
            fail(f"unauthenticated connect: unexpected HTTP {code}")
        ok(f"unauthenticated /events rejected (HTTP {code})")
    except websockets.exceptions.ConnectionClosed as e:
        if e.rcvd is None or e.rcvd.code != 1008:
            raise
        ok("unauthenticated /events rejected (close 1008)")

    headers = {"Authorization": f"Bearer {SECRET}"}
    try:
        async with websockets.connect(URL, additional_headers=headers, open_timeout=10) as ws:
            await roundtrip(ws, f"authenticated wss://{DOMAIN}/events round-trip")
            if SOAK:
                ok(f"soaking the socket for {SOAK}s...")
                # The library's protocol-level ping/pong (default every 20s)
                # runs underneath; a proxy-timeout kill surfaces as
                # ConnectionClosed out of the sleep or the second round-trip.
                await asyncio.sleep(SOAK)
                await roundtrip(ws, f"socket still live after {SOAK}s soak")
    except websockets.exceptions.InvalidStatus as e:
        fail(
            f"authenticated connect rejected (HTTP {e.response.status_code})"
            " — wrong ARK_AUTH_SECRET?"
        )


asyncio.run(main())
PY

ok "all checks passed for $DOMAIN"
