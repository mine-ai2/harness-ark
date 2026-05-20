"""In-process pub/sub for live event delivery.

Two subscription flavors:

- `subscribe(session_id)` — receive events targeted at one session. Used
  historically by the per-session WebSocket handler; still useful for any
  code path that wants to listen narrowly.
- `subscribe_all()` — receive every event regardless of session. Used by the
  unified per-client `/events` WebSocket.

A publish fans out to BOTH per-session subscribers and global subscribers.
Events that nobody is subscribed to are dropped — persistence is the DB's
job, this channel only carries live notifications.
"""

from __future__ import annotations

import asyncio


_subscribers: dict[str, list[asyncio.Queue]] = {}
_global_subscribers: list[asyncio.Queue] = []


def subscribe(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(session_id, []).append(q)
    return q


def subscribe_all() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _global_subscribers.append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    if session_id not in _subscribers:
        return
    try:
        _subscribers[session_id].remove(q)
    except ValueError:
        return
    if not _subscribers[session_id]:
        del _subscribers[session_id]


def unsubscribe_all(q: asyncio.Queue) -> None:
    try:
        _global_subscribers.remove(q)
    except ValueError:
        return


def publish(session_id: str, event: dict) -> None:
    for q in _subscribers.get(session_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    for q in _global_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def has_subscribers(session_id: str) -> bool:
    return bool(_subscribers.get(session_id)) or bool(_global_subscribers)
