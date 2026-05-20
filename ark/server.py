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
from .types import AssistantText, UploadMessage, message_from_row

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

    # ------------------------------------------------------------------
    # Unified per-client event stream + cross-session catch-up
    # ------------------------------------------------------------------

    @app.get("/events")
    def list_events(
        since_id: int | None = None,
        since_ms: int | None = None,
        limit: int = 200,
    ):
        """Catch-up query: persisted messages across every session, ordered by
        the monotonic message id. Use `since_id` for durable resume (the id
        the client last processed), or `since_ms` for a wall-clock-relative
        window. If neither is given, returns the most recent `limit` events."""

        limit = max(1, min(int(limit), 1000))
        sql = (
            "SELECT m.id, m.session_id, m.created_at, m.role, m.content_json, "
            "s.agent_name FROM messages m JOIN sessions s ON s.id = m.session_id"
        )
        where: list[str] = []
        params: list = []
        if since_id is not None:
            where.append("m.id > ?")
            params.append(int(since_id))
        if since_ms is not None:
            where.append("m.created_at >= ?")
            params.append(int(since_ms))
        if where:
            sql += " WHERE " + " AND ".join(where)
        if since_id is None and since_ms is None:
            # No cursor → return the *latest* `limit` rows. Order DESC, then
            # reverse client-side to keep ascending wire shape.
            sql += " ORDER BY m.id DESC LIMIT ?"
            params.append(limit + 1)
            rows = list(conn.execute(sql, params).fetchall())
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
        else:
            sql += " ORDER BY m.id ASC LIMIT ?"
            params.append(limit + 1)
            rows = list(conn.execute(sql, params).fetchall())
            has_more = len(rows) > limit
            rows = rows[:limit]

        events: list[dict] = []
        for r in rows:
            msg = message_from_row(r["role"], json.loads(r["content_json"]))
            wire = _message_to_json(msg)
            events.append(
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "agent_name": r["agent_name"],
                    "created_at": r["created_at"],
                    **wire,
                }
            )
        return {
            "events": events,
            "next_since_id": events[-1]["id"] if events else since_id,
            "has_more": has_more,
        }

    @app.websocket("/events")
    async def ws_events(ws: WebSocket):
        """Single per-client WebSocket. Receives events for every session of
        every agent. Commands target a session via the `session_id` field."""

        token = ws.headers.get("authorization", "") or _qs_token(ws)
        if not _check_bearer(token, config.server.auth_secret):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = broker.subscribe_all()

        async def forwarder() -> None:
            try:
                while True:
                    event = await queue.get()
                    try:
                        await ws.send_json(event)
                    except Exception:  # noqa: BLE001
                        return
            except asyncio.CancelledError:
                return

        fwd_task = asyncio.create_task(forwarder())
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json(
                        {"type": "error", "code": "other", "message": "invalid JSON"}
                    )
                    continue
                t = cmd.get("type")
                sid = cmd.get("session_id")
                if t == "stop":
                    # v1: no-op (mid-turn cancellation isn't supported).
                    continue
                if t != "user_message":
                    await ws.send_json(
                        {
                            "type": "error",
                            "code": "other",
                            "message": f"unsupported command type: {t!r}",
                        }
                    )
                    continue
                if not sid or not isinstance(sid, str):
                    await ws.send_json(
                        {
                            "type": "error",
                            "code": "other",
                            "message": "user_message requires a string `session_id`",
                        }
                    )
                    continue
                row = conn.execute(
                    "SELECT agent_name FROM sessions WHERE id = ?", (sid,)
                ).fetchone()
                if row is None:
                    await ws.send_json(
                        {
                            "type": "error",
                            "session_id": sid,
                            "code": "other",
                            "message": "unknown session",
                        }
                    )
                    continue
                agent = config.agents.get(row["agent_name"])
                if agent is None:
                    await ws.send_json(
                        {
                            "type": "error",
                            "session_id": sid,
                            "code": "other",
                            "message": f"unknown agent {row['agent_name']!r}",
                        }
                    )
                    continue
                # Spawn the turn as a background task — multiple sessions can
                # have turns running concurrently. Their events all flow back
                # through this same WS via the broker subscription.
                asyncio.create_task(
                    runtime.run_and_publish(
                        conn=conn,
                        config=config,
                        agent=agent,
                        session_id=sid,
                        user_text=cmd.get("text", ""),
                    )
                )
        except WebSocketDisconnect:
            return
        finally:
            fwd_task.cancel()
            broker.unsubscribe_all(queue)

    return app


def _check_bearer(header: str, expected: str) -> bool:
    if not header.lower().startswith("bearer "):
        return False
    return header[7:].strip() == expected


def _qs_token(ws: WebSocket) -> str:
    token = ws.query_params.get("token")
    return f"Bearer {token}" if token else ""


def _message_to_json(msg: Any) -> dict:
    if not is_dataclass(msg):
        return {"kind": type(msg).__name__, "data": {}}
    # An AssistantText with `injected_from` set represents a cross-session
    # injection (see post_to_session). Surface it under a distinct kind so
    # clients can render it consistently with the live `injected_message`
    # WS event, rather than seeing two different shapes for the same thing.
    if isinstance(msg, AssistantText) and msg.injected_from:
        return {
            "kind": "InjectedMessage",
            "data": {"text": msg.text, "from_session_id": msg.injected_from},
        }
    data = asdict(msg)
    # `thought_signature` is opaque bytes used internally to round-trip Gemini
    # thinking traces back to the model. Not meaningful to clients, and
    # FastAPI's default JSON encoder treats bytes as UTF-8 strings, which
    # explodes for binary content. Drop it before serializing.
    data.pop("thought_signature", None)
    return {"kind": type(msg).__name__, "data": data}
