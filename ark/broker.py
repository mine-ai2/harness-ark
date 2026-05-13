"""In-process pub/sub for live cross-session messages.

A websocket handler subscribes when a client connects to a session; tools (or
the runtime) publish events keyed by session id. If no subscriber is present,
events are dropped — the persisted history is the source of truth, this
channel only delivers live notifications.
"""

from __future__ import annotations

import asyncio


_subscribers: dict[str, list[asyncio.Queue]] = {}


def subscribe(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(session_id, []).append(q)
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


def publish(session_id: str, event: dict) -> None:
    for q in _subscribers.get(session_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def has_subscribers(session_id: str) -> bool:
    return bool(_subscribers.get(session_id))
