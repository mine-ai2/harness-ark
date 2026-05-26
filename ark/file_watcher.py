"""Per-subject filesystem watcher.

A single `Observer` (from `watchdog`) hosts one watch per *subject* — where a
subject is either a project (its root directory) or an agent's workspace.
Filesystem events get coalesced briefly (so editor-save bursts collapse to
one notification), filtered through an ignore-list (`.git`, `node_modules`,
etc.), and published to the broker as `project_file_changed` /
`workspace_file_changed` events so unified `/events` WS subscribers see
them live.

Each subject is identified by a `(kind, id)` pair. The wire-event type and
the id field name vary by kind:

| kind        | event `type`              | id field        |
|-------------|---------------------------|-----------------|
| `project`   | `project_file_changed`    | `project_id`    |
| `workspace` | `workspace_file_changed`  | `agent_name`    |
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import broker

# Ignore anything whose absolute path contains any of these segments.
_IGNORE_SEGMENTS = (
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    ".DS_Store",
    ".idea",
    ".vscode",
)

_COALESCE_WINDOW_S = 0.2

# kind → (wire event type, id field name)
_EVENT_SHAPE = {
    "project": ("project_file_changed", "project_id"),
    "workspace": ("workspace_file_changed", "agent_name"),
}


def _should_ignore(path: str) -> bool:
    return any(seg in path.split("/") for seg in _IGNORE_SEGMENTS)


class FileWatcher:
    """One Observer, many subject watches added/removed at runtime."""

    def __init__(self) -> None:
        self._observer: Observer | None = None
        self._watches: dict[tuple[str, str], object] = {}
        self._pending: dict[tuple[str, str, str, str], float] = {}
        self._pending_lock = threading.Lock()
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._observer is not None:
            return
        self._observer = Observer()
        self._observer.start()
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        finally:
            self._observer = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

    def watch(self, kind: str, subject_id: str, root: str) -> None:
        if self._observer is None:
            return
        if kind not in _EVENT_SHAPE:
            raise ValueError(f"unknown subject kind: {kind!r}")
        key = (kind, subject_id)
        if key in self._watches:
            return
        path = Path(root)
        if not path.is_dir():
            return
        handler = _Handler(kind, subject_id, self)
        watch = self._observer.schedule(handler, str(path), recursive=True)
        self._watches[key] = watch

    def unwatch(self, kind: str, subject_id: str) -> None:
        if self._observer is None:
            return
        watch = self._watches.pop((kind, subject_id), None)
        if watch is not None:
            self._observer.unschedule(watch)

    # Called from the watchdog thread.
    def _enqueue(self, kind: str, subject_id: str, rel_path: str, change: str) -> None:
        key = (kind, subject_id, rel_path, change)
        with self._pending_lock:
            self._pending[key] = time.monotonic()

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_COALESCE_WINDOW_S)
                now = time.monotonic()
                ready: list[tuple[str, str, str, str]] = []
                with self._pending_lock:
                    for key, ts in list(self._pending.items()):
                        if now - ts >= _COALESCE_WINDOW_S:
                            ready.append(key)
                            del self._pending[key]
                for kind, subject_id, rel_path, change in ready:
                    event_type, id_field = _EVENT_SHAPE[kind]
                    broker.publish(
                        # Topic naming: not used by per-session subscribers,
                        # but kept distinct from any session id space.
                        f"{kind}:{subject_id}",
                        {
                            "type": event_type,
                            id_field: subject_id,
                            "path": rel_path,
                            "change": change,
                        },
                    )
        except asyncio.CancelledError:
            return


class _Handler(FileSystemEventHandler):
    def __init__(self, kind: str, subject_id: str, watcher: FileWatcher) -> None:
        self.kind = kind
        self.subject_id = subject_id
        self.watcher = watcher
        self._root: Path | None = None

    def _rel(self, abs_path: str) -> str | None:
        if _should_ignore(abs_path):
            return None
        try:
            root = self._root
            if root is None:
                watch = self.watcher._watches.get((self.kind, self.subject_id))
                if watch is None:
                    return None
                self._root = Path(watch.path)
                root = self._root
            return str(Path(abs_path).relative_to(root))
        except ValueError:
            return None

    def _send(self, abs_path: str, change: str) -> None:
        rel = self._rel(abs_path)
        if rel is None or rel == ".":
            return
        self.watcher._enqueue(self.kind, self.subject_id, rel, change)

    def on_created(self, event: FileSystemEvent) -> None:
        self._send(event.src_path, "created")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._send(event.src_path, "deleted")

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            # Directory-modified events are noisy and not actionable on their
            # own (the creates/deletes inside fire separately).
            return
        self._send(event.src_path, "modified")

    def on_moved(self, event: FileSystemEvent) -> None:
        self._send(event.src_path, "deleted")
        dest = getattr(event, "dest_path", None)
        if dest:
            self._send(dest, "created")


_watcher: FileWatcher | None = None


def get_watcher() -> FileWatcher:
    global _watcher
    if _watcher is None:
        _watcher = FileWatcher()
    return _watcher
