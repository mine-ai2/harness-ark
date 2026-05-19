"""FastAPI server: REST + WebSocket session interaction + scheduler lifecycle."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from . import broker, db, runtime, skills, workspace as ws
from .config import Config
from .scheduler import Scheduler
from .types import (
    AssistantTurnEnd,
    RunEnd,
    TextDelta,
    ThinkingDelta,
    ToolCallEvent,
    ToolResultEvent,
    UploadMessage,
)

UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB v1 cap


def create_app(config: Config) -> FastAPI:
    conn = db.init_db()
    skills.discover(list(config.agents))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        sched = Scheduler(conn, config)
        sched.start()
        try:
            yield
        finally:
            await sched.stop()

    app = FastAPI(title="Ark", lifespan=lifespan)
    app.state.config = config
    app.state.conn = conn

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not _check_bearer(request.headers.get("authorization", ""), config.server.auth_secret):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/agents")
    def list_agents():
        return [
            {
                "name": a.name,
                "provider": a.provider,
                "provider_type": config.providers[a.provider].provider_type,
                "model": a.model,
                "workspace": str(a.workspace),
            }
            for a in config.agents.values()
        ]

    @app.get("/agents/{name}")
    def get_agent(name: str):
        agent = config.agents.get(name)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        provider_cfg = config.providers[agent.provider]
        heartbeat = conn.execute(
            "SELECT heartbeat_seconds FROM agent_state WHERE agent_name = ?", (name,)
        ).fetchone()
        crons = conn.execute(
            "SELECT id, expr, prompt FROM crons WHERE agent_name = ? AND enabled = 1",
            (name,),
        ).fetchall()
        return {
            "name": agent.name,
            "provider": agent.provider,
            "provider_type": provider_cfg.provider_type,
            "model": agent.model,
            "workspace": str(agent.workspace),
            "always_loaded_skills": agent.always_loaded_skills,
            "heartbeat_seconds": heartbeat["heartbeat_seconds"] if heartbeat else None,
            "crons": [dict(r) for r in crons],
        }

    @app.put("/agents/{name}/heartbeat")
    async def set_heartbeat(name: str, body: dict):
        if name not in config.agents:
            raise HTTPException(404, "unknown agent")
        seconds = body.get("interval_seconds")
        if seconds is None:
            conn.execute(
                "INSERT INTO agent_state(agent_name, heartbeat_seconds) VALUES (?, NULL) "
                "ON CONFLICT(agent_name) DO UPDATE SET heartbeat_seconds = NULL",
                (name,),
            )
        else:
            conn.execute(
                "INSERT INTO agent_state(agent_name, heartbeat_seconds) VALUES (?, ?) "
                "ON CONFLICT(agent_name) DO UPDATE SET heartbeat_seconds = excluded.heartbeat_seconds",
                (name, int(seconds)),
            )
        return {"ok": True}

    @app.get("/agents/{name}/crons")
    def list_crons(name: str):
        if name not in config.agents:
            raise HTTPException(404, "unknown agent")
        rows = conn.execute(
            "SELECT id, expr, prompt, enabled FROM crons WHERE agent_name = ? ORDER BY id",
            (name,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.put("/agents/{name}/crons/{cron_id}")
    def upsert_cron(name: str, cron_id: str, body: dict):
        if name not in config.agents:
            raise HTTPException(404, "unknown agent")
        expr = body.get("expr")
        prompt = body.get("prompt")
        if not expr or not prompt:
            raise HTTPException(400, "expr and prompt are required")
        try:
            from croniter import croniter

            croniter(expr)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"invalid cron expression: {e}")
        conn.execute(
            "INSERT INTO crons(agent_name, id, expr, prompt, enabled) VALUES (?,?,?,?,1) "
            "ON CONFLICT(agent_name, id) DO UPDATE SET expr=excluded.expr, prompt=excluded.prompt, enabled=1",
            (name, cron_id, expr, prompt),
        )
        return {"ok": True}

    @app.delete("/agents/{name}/crons/{cron_id}")
    def delete_cron(name: str, cron_id: str):
        cur = conn.execute(
            "DELETE FROM crons WHERE agent_name = ? AND id = ?", (name, cron_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "unknown cron")
        return {"ok": True}

    @app.get("/agents/{name}/sessions")
    def list_sessions(name: str, kind: str | None = None, limit: int = 50):
        if name not in config.agents:
            raise HTTPException(404, "unknown agent")
        return runtime.list_sessions(conn, name, kind=kind, limit=limit)

    @app.post("/agents/{name}/sessions")
    async def create_session(name: str, request: Request):
        if name not in config.agents:
            raise HTTPException(404, "unknown agent")
        # Body is optional. Accept either an empty request or a JSON object
        # with `{"context": "..."}` to seed the session's first SessionContext
        # message.
        ctx_text = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                ctx_text = (body.get("context") or "").strip()
        except Exception:
            pass  # no/invalid body → just create the session
        sid = runtime.create_session(conn, name, "conversational")
        if ctx_text:
            runtime.append_context(conn, sid, ctx_text)
        return {"id": sid}

    @app.post("/agents/{name}/sessions/{sid}/context")
    async def append_session_context(name: str, sid: str, request: Request):
        if not runtime.session_exists(conn, sid, name):
            raise HTTPException(404, "unknown session")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "expected JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        text = (body.get("context") or "").strip()
        if not text:
            raise HTTPException(400, "missing or empty 'context'")
        count = runtime.append_context(conn, sid, text)
        return {"ok": True, "count": count}

    @app.delete("/agents/{name}/sessions/{sid}")
    def delete_session(name: str, sid: str):
        if not runtime.session_exists(conn, sid, name):
            raise HTTPException(404, "unknown session")
        runtime.delete_session(conn, sid)
        return {"ok": True}

    @app.get("/agents/{name}/sessions/{sid}/history")
    def get_history(name: str, sid: str):
        if not runtime.session_exists(conn, sid, name):
            raise HTTPException(404, "unknown session")
        return [_message_to_json(m) for m in runtime.load_history(conn, sid)]

    # ------------------------------------------------------------------
    # File transfer: shared uploads bucket + arbitrary workspace downloads
    # ------------------------------------------------------------------

    @app.post("/agents/{name}/sessions/{sid}/uploads")
    async def upload_file(name: str, sid: str, file: UploadFile = File(...)):
        agent = config.agents.get(name)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        if not runtime.session_exists(conn, sid, name):
            raise HTTPException(404, "unknown session")
        if not file.filename:
            raise HTTPException(400, "missing filename")

        try:
            dest = ws.reserve_upload_filename(agent.workspace, file.filename)
        except ws.WorkspaceError as e:
            raise HTTPException(400, str(e))

        # Stream to disk, enforcing the cap.
        written = 0
        chunk_size = 64 * 1024
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"upload exceeds {UPLOAD_MAX_BYTES} bytes")
                out.write(chunk)

        rel = ws.relative_to_workspace(agent.workspace, dest)
        runtime.append_message(
            conn,
            sid,
            UploadMessage(path=rel, original_name=file.filename, size=written),
        )
        return {"path": rel, "size": written, "original_name": file.filename}

    @app.get("/agents/{name}/sessions/{sid}/uploads")
    def list_uploads(name: str, sid: str):
        agent = config.agents.get(name)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        if not runtime.session_exists(conn, sid, name):
            raise HTTPException(404, "unknown session")
        uploads = ws.uploads_dir(agent.workspace)
        if not uploads.is_dir():
            return []
        entries = []
        for p in sorted(uploads.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
            if p.is_file():
                stat = p.stat()
                entries.append(
                    {"path": f"{ws.UPLOADS_DIRNAME}/{p.name}", "size": stat.st_size, "mtime": int(stat.st_mtime)}
                )
        return entries

    @app.get("/agents/{name}/files/{path:path}")
    def download_file(name: str, path: str):
        agent = config.agents.get(name)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        try:
            full = ws.resolve(agent.workspace, path)
        except ws.WorkspaceError as e:
            raise HTTPException(400, str(e))
        if not full.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(full, filename=full.name)

    @app.websocket("/agents/{name}/sessions/{sid}")
    async def ws_session(ws: WebSocket, name: str, sid: str):
        token = ws.headers.get("authorization", "") or _qs_token(ws)
        if not _check_bearer(token, config.server.auth_secret):
            await ws.close(code=1008)
            return
        agent = config.agents.get(name)
        if agent is None or not runtime.session_exists(conn, sid, name):
            await ws.close(code=1003)
            return
        await ws.accept()
        queue = broker.subscribe(sid)
        injected_task = asyncio.create_task(_forward_injected(ws, queue))
        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "stop":
                    continue  # mid-turn cancellation: v1 no-op
                if t != "user_message":
                    await ws.send_json({"type": "error", "message": "unsupported message type"})
                    continue
                try:
                    async for evt in runtime.run_user_turn(
                        conn=conn,
                        config=config,
                        agent=agent,
                        session_id=sid,
                        user_text=msg.get("text", ""),
                    ):
                        await ws.send_json(_event_to_wire(evt))
                except Exception as e:  # noqa: BLE001
                    await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
        except WebSocketDisconnect:
            return
        finally:
            injected_task.cancel()
            broker.unsubscribe(sid, queue)

    return app


async def _forward_injected(ws: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            event = await queue.get()
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                return
    except asyncio.CancelledError:
        return


def _check_bearer(header: str, expected: str) -> bool:
    if not header.lower().startswith("bearer "):
        return False
    return header[7:].strip() == expected


def _qs_token(ws: WebSocket) -> str:
    token = ws.query_params.get("token")
    return f"Bearer {token}" if token else ""


def _event_to_wire(evt: Any) -> dict:
    if isinstance(evt, TextDelta):
        return {"type": "assistant_delta", "text": evt.text}
    if isinstance(evt, ThinkingDelta):
        return {"type": "thinking", "delta": evt.text}
    if isinstance(evt, AssistantTurnEnd):
        return {"type": "assistant_message", "text": evt.text}
    if isinstance(evt, ToolCallEvent):
        return {"type": "tool_call", "id": evt.id, "name": evt.name, "input": evt.input}
    if isinstance(evt, ToolResultEvent):
        return {
            "type": "tool_result",
            "id": evt.call_id,
            "output": evt.output,
            "error": evt.is_error,
        }
    if isinstance(evt, RunEnd):
        return {"type": "done", "stop_reason": evt.stop_reason}
    if is_dataclass(evt):
        return {"type": type(evt).__name__, **asdict(evt)}
    return {"type": "unknown"}


def _message_to_json(msg: Any) -> dict:
    return {"kind": type(msg).__name__, "data": asdict(msg) if is_dataclass(msg) else {}}
