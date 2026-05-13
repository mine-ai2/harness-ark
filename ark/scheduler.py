"""Heartbeat + cron scheduler.

A long-running asyncio task that wakes once per second, decides which timers
should fire, and starts a fresh session for each. Heartbeat intervals come
from `agent_state.heartbeat_seconds`; cron entries come from the `crons`
table (managed by the schedule meta-tool).

State is in-memory: `last_fired` per agent / per cron entry. On startup, all
`last_fired` values are seeded to "now" so timers don't fire immediately.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timezone

from croniter import croniter

from . import paths, runtime
from .config import Config


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config
        self.last_heartbeat: dict[str, float] = {}
        self.last_cron: dict[tuple[str, str], float] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        now = time.time()
        for name in self.config.agents:
            self.last_heartbeat[name] = now
        for row in self.conn.execute(
            "SELECT agent_name, id FROM crons WHERE enabled = 1"
        ).fetchall():
            self.last_cron[(row["agent_name"], row["id"])] = now

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] tick error: {e}", file=sys.stderr)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        now = time.time()
        # heartbeats
        for name, agent in self.config.agents.items():
            interval = self._heartbeat_interval(name)
            if not interval:
                continue
            if now - self.last_heartbeat.get(name, now) >= interval:
                self.last_heartbeat[name] = now
                asyncio.create_task(self._fire_heartbeat(name))

        # crons
        rows = self.conn.execute(
            "SELECT agent_name, id, expr, prompt FROM crons WHERE enabled = 1"
        ).fetchall()
        now_dt = datetime.now(timezone.utc)
        for row in rows:
            key = (row["agent_name"], row["id"])
            last = self.last_cron.get(key, now)
            last_dt = datetime.fromtimestamp(last, tz=timezone.utc)
            try:
                next_dt = croniter(row["expr"], last_dt).get_next(datetime)
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] cron {row['id']!r}: {e}", file=sys.stderr)
                continue
            if now_dt >= next_dt:
                self.last_cron[key] = now
                asyncio.create_task(
                    self._fire_cron(row["agent_name"], row["id"], row["prompt"])
                )

    def _heartbeat_interval(self, agent_name: str) -> int | None:
        row = self.conn.execute(
            "SELECT heartbeat_seconds FROM agent_state WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
        if row is None:
            return None
        return row["heartbeat_seconds"]

    async def _fire_heartbeat(self, agent_name: str) -> None:
        agent = self.config.agents.get(agent_name)
        if agent is None:
            return
        prompt_path = paths.agent_dir(agent_name) / "heartbeat_prompt.md"
        prompt = (
            prompt_path.read_text()
            if prompt_path.exists()
            else "This is your scheduled heartbeat. Check on anything that needs attention."
        )
        await self._drive(agent_name, "heartbeat", prompt)

    async def _fire_cron(self, agent_name: str, cron_id: str, prompt: str) -> None:
        await self._drive(agent_name, "cron", prompt)

    async def _drive(self, agent_name: str, kind: str, prompt: str) -> None:
        agent = self.config.agents.get(agent_name)
        if agent is None:
            return
        sid = runtime.create_session(self.conn, agent_name, kind=kind)
        try:
            async for _ in runtime.run_user_turn(
                conn=self.conn,
                config=self.config,
                agent=agent,
                session_id=sid,
                user_text=prompt,
            ):
                pass
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] {kind} session {sid} error: {e}", file=sys.stderr)
