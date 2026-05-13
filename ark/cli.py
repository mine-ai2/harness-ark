"""CLI client: `ark agents`, `ark sessions`, `ark chat`. Talks to the local
Ark server over REST + WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Any

import httpx
import websockets

from . import config, paths


def _client_settings() -> tuple[str, str]:
    """Return (base_url, auth_secret). Reads ~/.ark/config.json."""
    cfg = config.load()
    host = cfg.server.host
    # `0.0.0.0` / `::` are server bind addresses, not dial-able client targets.
    # In the containerized dev setup the server binds 0.0.0.0 and the host's
    # port-forward exposes 127.0.0.1:<port>.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    base_url = f"http://{host}:{cfg.server.port}"
    return base_url, cfg.server.auth_secret


def _headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


def cmd_agents(_args: argparse.Namespace) -> int:
    base_url, secret = _client_settings()
    r = httpx.get(f"{base_url}/agents", headers=_headers(secret), timeout=10)
    r.raise_for_status()
    for a in r.json():
        print(f"{a['name']:20s}  {a['provider']:12s}  {a['model']}")
    return 0


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def cmd_sessions(args: argparse.Namespace) -> int:
    base_url, secret = _client_settings()
    params: dict[str, Any] = {"limit": args.limit}
    if args.kind:
        params["kind"] = args.kind
    r = httpx.get(
        f"{base_url}/agents/{args.agent}/sessions",
        headers=_headers(secret),
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        print("(no sessions)")
        return 0
    for row in rows:
        ts = datetime.fromtimestamp(row["created_at"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{row['id']}  {row['kind']:14s}  {ts}")
    return 0


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _Ui:
    """Renders streaming assistant text plus a transient animated status line.

    Status (tool activity, thinking) lives on its own line below the cursor,
    drawn over with ANSI escape codes. The moment real assistant text starts
    streaming, the status line is wiped — and at end-of-turn it is wiped for
    good. The result: the transcript holds only the conversation, with a
    live indicator while the agent is working.
    """

    def __init__(self) -> None:
        self.is_tty = sys.stderr.isatty()
        self.status_text = ""
        self.status_visible = False
        self.text_pending_newline = False
        self.spinner_task: asyncio.Task | None = None

    def assistant_delta(self, text: str) -> None:
        self._wipe_status()
        sys.stdout.write(text)
        sys.stdout.flush()
        if text:
            self.text_pending_newline = not text.endswith("\n")

    def assistant_turn_end(self) -> None:
        if self.text_pending_newline:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.text_pending_newline = False

    def status(self, text: str) -> None:
        self.status_text = text
        if not self.is_tty:
            sys.stderr.write(f"[{text}]\n")
            sys.stderr.flush()
            return
        if self.text_pending_newline:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.text_pending_newline = False
        self.status_visible = True
        self._render(_SPINNER[0])
        if self.spinner_task is None or self.spinner_task.done():
            self.spinner_task = asyncio.create_task(self._spinner_loop())

    def error_line(self, text: str) -> None:
        self._wipe_status()
        if self.text_pending_newline:
            sys.stdout.write("\n")
            self.text_pending_newline = False
        sys.stderr.write(f"\033[31m{text}\033[0m\n")
        sys.stderr.flush()

    def done(self) -> None:
        self._wipe_status()
        self.assistant_turn_end()

    def _wipe_status(self) -> None:
        if self.spinner_task and not self.spinner_task.done():
            self.spinner_task.cancel()
        self.spinner_task = None
        if self.status_visible and self.is_tty:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        self.status_visible = False

    def _render(self, glyph: str) -> None:
        if not self.is_tty:
            return
        sys.stderr.write(f"\r\033[K\033[2m{glyph} {self.status_text}\033[0m")
        sys.stderr.flush()

    async def _spinner_loop(self) -> None:
        i = 1
        try:
            while self.status_visible:
                await asyncio.sleep(0.1)
                if not self.status_visible:
                    break
                self._render(_SPINNER[i])
                i = (i + 1) % len(_SPINNER)
        except asyncio.CancelledError:
            pass


def _handle_event(ui: _Ui, evt: dict) -> None:
    t = evt.get("type")
    if t == "assistant_delta":
        ui.assistant_delta(evt.get("text", ""))
    elif t == "assistant_message":
        ui.assistant_turn_end()
    elif t == "thinking":
        ui.status("thinking")
    elif t == "tool_call":
        ui.status(f"running {evt.get('name', '?')}")
    elif t == "tool_result":
        if evt.get("error"):
            preview = (evt.get("output") or "").splitlines()[0][:80] if evt.get("output") else ""
            ui.error_line(f"[tool error: {preview}]" if preview else "[tool error]")
        ui.status("thinking")
    elif t == "error":
        ui.error_line(f"[server error: {evt.get('message')}]")
    elif t == "done":
        ui.done()


async def _chat(agent: str, session_id: str | None, history_only: bool = False) -> int:
    base_url, secret = _client_settings()
    headers = _headers(secret)

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as http:
        if session_id is None:
            r = await http.post(f"/agents/{agent}/sessions")
            r.raise_for_status()
            session_id = r.json()["id"]
            print(f"[new session: {session_id}]", file=sys.stderr)
        else:
            r = await http.get(f"/agents/{agent}/sessions/{session_id}/history")
            r.raise_for_status()
            for m in r.json():
                _print_history_entry(m)
            if history_only:
                return 0

        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{ws_url}/agents/{agent}/sessions/{session_id}?token={secret}"
        async with websockets.connect(url) as ws:
            print(f"[connected — type your message, Ctrl-D to exit]", file=sys.stderr)
            ui = _Ui()
            stop = asyncio.Event()
            turn_done = asyncio.Event()
            turn_done.set()  # ready for first input

            async def reader():
                try:
                    async for raw in ws:
                        evt = json.loads(raw)
                        _handle_event(ui, evt)
                        if evt.get("type") == "done":
                            turn_done.set()
                except websockets.ConnectionClosed:
                    pass
                finally:
                    stop.set()
                    turn_done.set()  # unblock writer so it can exit

            async def writer():
                loop = asyncio.get_event_loop()
                while not stop.is_set():
                    await turn_done.wait()
                    if stop.is_set():
                        return
                    print("you> ", end="", flush=True, file=sys.stderr)
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if line == "":  # EOF
                        await ws.close()
                        return
                    text = line.strip()
                    if not text:
                        continue
                    turn_done.clear()
                    ui.status("thinking")
                    await ws.send(json.dumps({"type": "user_message", "text": text}))

            await asyncio.gather(reader(), writer())
    return 0


def _print_history_entry(m: dict) -> None:
    kind = m.get("kind")
    data = m.get("data", {})
    if kind == "UserText":
        sys.stderr.write(f"\nyou> {data.get('text')}\n")
    elif kind == "AssistantText":
        sys.stdout.write(data.get("text", ""))
        sys.stdout.write("\n")
    elif kind == "ToolCall":
        sys.stderr.write(f"[tool {data.get('name')}(...)]\n")
    elif kind == "ToolResult":
        out = data.get("output", "")
        if len(out) > 400:
            out = out[:400] + "…"
        prefix = "tool error" if data.get("is_error") else "tool result"
        sys.stderr.write(f"[{prefix}: {out}]\n")


def cmd_chat(args: argparse.Namespace) -> int:
    return asyncio.run(_chat(args.agent, args.session))


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(_args: argparse.Namespace) -> int:
    import uvicorn

    from . import bootstrap, server

    if not paths.config_path().exists():
        print(f"no config at {paths.config_path()}. Run `python -m ark init` first.", file=sys.stderr)
        return 1
    cfg = config.load()
    bootstrap.bootstrap(cfg)  # idempotent — ensures agent dirs exist on every start
    app = server.create_app(cfg)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")
    return 0


def cmd_heartbeat_set(args: argparse.Namespace) -> int:
    base_url, secret = _client_settings()
    body = {"interval_seconds": None if args.seconds in (0, "0") else int(args.seconds)}
    r = httpx.put(
        f"{base_url}/agents/{args.agent}/heartbeat",
        headers=_headers(secret),
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    print(f"heartbeat: {'disabled' if body['interval_seconds'] is None else str(body['interval_seconds']) + 's'}")
    return 0


def cmd_cron_list(args: argparse.Namespace) -> int:
    base_url, secret = _client_settings()
    r = httpx.get(
        f"{base_url}/agents/{args.agent}/crons", headers=_headers(secret), timeout=10
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        print("(no crons)")
        return 0
    for c in rows:
        status = "" if c["enabled"] else " [disabled]"
        print(f"{c['id']}: {c['expr']}{status} — {c['prompt'][:60]}")
    return 0


def cmd_cron_set(args: argparse.Namespace) -> int:
    base_url, secret = _client_settings()
    prompt = args.prompt_file.read_text() if args.prompt_file else args.prompt
    if not prompt:
        print("--prompt or --prompt-file is required", file=sys.stderr)
        return 1
    r = httpx.put(
        f"{base_url}/agents/{args.agent}/crons/{args.id}",
        headers=_headers(secret),
        json={"expr": args.expr, "prompt": prompt},
        timeout=10,
    )
    if r.status_code >= 400:
        print(f"error: {r.text}", file=sys.stderr)
        return 1
    print(f"cron '{args.id}' saved")
    return 0


def cmd_cron_remove(args: argparse.Namespace) -> int:
    base_url, secret = _client_settings()
    r = httpx.delete(
        f"{base_url}/agents/{args.agent}/crons/{args.id}",
        headers=_headers(secret),
        timeout=10,
    )
    if r.status_code == 404:
        print("not found", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(f"cron '{args.id}' removed")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def cmd_init(_args: argparse.Namespace) -> int:
    from . import bootstrap, db

    if not paths.config_path().exists():
        print(
            f"no config at {paths.config_path()}. Create it before running init.",
            file=sys.stderr,
        )
        return 1
    cfg = config.load()
    bootstrap.bootstrap(cfg)
    conn = db.init_db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    print(f"ark home: {paths.ark_home()}")
    print(f"agents:   {', '.join(cfg.agents) or '(none configured)'}")
    print(f"db:       {paths.db_path()} (schema v{version})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ark")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="bootstrap ARK_HOME and apply DB migrations").set_defaults(
        func=cmd_init
    )

    sub.add_parser("agents", help="list configured agents").set_defaults(func=cmd_agents)

    sess = sub.add_parser("sessions", help="list an agent's sessions")
    sess.add_argument("agent")
    sess.add_argument("--kind", help="filter by session kind")
    sess.add_argument("--limit", type=int, default=50)
    sess.set_defaults(func=cmd_sessions)

    chat = sub.add_parser("chat", help="open an interactive session")
    chat.add_argument("agent")
    chat.add_argument("--session", help="resume an existing session id")
    chat.set_defaults(func=cmd_chat)

    serve = sub.add_parser("serve", help="run the Ark server")
    serve.set_defaults(func=cmd_serve)

    hb = sub.add_parser("heartbeat", help="set or disable an agent's heartbeat")
    hb.add_argument("agent")
    hb.add_argument("seconds", help="interval in seconds (0 to disable)")
    hb.set_defaults(func=cmd_heartbeat_set)

    cron = sub.add_parser("cron", help="manage an agent's cron entries")
    cron_sub = cron.add_subparsers(dest="cron_command", required=True)

    cron_list = cron_sub.add_parser("list", help="list crons for an agent")
    cron_list.add_argument("agent")
    cron_list.set_defaults(func=cmd_cron_list)

    cron_set = cron_sub.add_parser("set", help="add or update a cron entry")
    cron_set.add_argument("agent")
    cron_set.add_argument("id")
    cron_set.add_argument("expr", help="5-field UNIX cron expression")
    cron_set.add_argument("--prompt", help="inline prompt body")
    cron_set.add_argument(
        "--prompt-file", type=argparse.FileType("r"), help="read prompt from file"
    )
    cron_set.set_defaults(func=cmd_cron_set)

    cron_remove = cron_sub.add_parser("remove", help="remove a cron entry")
    cron_remove.add_argument("agent")
    cron_remove.add_argument("id")
    cron_remove.set_defaults(func=cmd_cron_remove)

    return parser
