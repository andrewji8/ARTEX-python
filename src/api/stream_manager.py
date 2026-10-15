"""SSE ConnectionManager — Pub/Sub for broadcasting engine events to clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Singleton pub/sub manager for SSE connections.

    Architecture:
        task_id → List[asyncio.Queue[event_data]]

    Each connected SSE client owns a dedicated asyncio.Queue.
    The Engine (or any background task) calls broadcast() to push events.
    Consumers drain their queue in a non-blocking async loop.

    Supports Last-Event-ID reconnection: on reconnect, the manager replays
    the N most recent events (configurable via max_history_per_task).
    """

    def __init__(self, *, max_history_per_task: int = 200) -> None:
        self._connections: Dict[str, list[asyncio.Queue[Any]]] = {}
        self._lock = asyncio.Lock()
        self._history: Dict[str, list[tuple[str, Any]]] = {}  # task_id → [(event_id, data)]
        self._max_history = max_history_per_task
        self._instance_id = uuid4()

    # ---- registration ----

    async def connect(self, task_id: UUID) -> asyncio.Queue[Any]:
        """Create and register a new queue for the given task. Returns the queue."""
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
        async with self._lock:
            key = str(task_id)
            if key not in self._connections:
                self._connections[key] = []
            self._connections[key].append(q)
            logger.info("SSE client connected: task_id=%s (total=%d)", key, len(self._connections[key]))
        return q

    async def disconnect(self, task_id: UUID, queue: asyncio.Queue[Any]) -> None:
        """Remove a queue from the connection set."""
        async with self._lock:
            key = str(task_id)
            if key in self._connections:
                try:
                    self._connections[key].remove(queue)
                except ValueError:
                    pass
                if not self._connections[key]:
                    del self._connections[key]
                logger.info("SSE client disconnected: task_id=%s (remaining=%d)", key, len(self._connections.get(key, [])))

    # ---- broadcast ----

    async def broadcast(self, task_id: UUID, data: Any, *, event_type: str = "activity", event_id: Optional[str] = None) -> int:
        """
        Push an event to all subscribers for the given task_id.
        Returns the number of queues that received the event.
        """
        key = str(task_id)
        eid = event_id or f"{int(asyncio.get_event_loop().time() * 1000)}"

        # Store in history for reconnection support
        async with self._lock:
            if key not in self._history:
                self._history[key] = []
            self._history[key].append((eid, data))
            # Trim history
            if len(self._history[key]) > self._max_history:
                self._history[key] = self._history[key][-self._max_history:]

        sent = 0
        async with self._lock:
            queues = list(self._connections.get(key, []))

        for q in queues:
            try:
                q.put_nowait({
                    "event": event_type,
                    "id": eid,
                    "data": data,
                })
                sent += 1
            except asyncio.QueueFull:
                logger.warning("SSE queue full for task_id=%s, dropping event", key)

        return sent

    async def broadcast_many(self, task_ids: list[UUID], data: Any, *, event_type: str = "activity") -> int:
        """Broadcast the same event to multiple task queues."""
        total = 0
        for tid in task_ids:
            total += await self.broadcast(tid, data, event_type=event_type)
        return total

    # ---- replay for reconnection ----

    async def get_history(self, task_id: UUID, after_event_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        """
        Return recent events for reconnection support (Last-Event-ID).
        If after_event_id is given, returns events with id > after_event_id.
        """
        key = str(task_id)
        async with self._lock:
            history = list(self._history.get(key, []))

        if after_event_id:
            # Find index of the after_event_id and return subsequent events
            found = False
            for i, (eid, _) in enumerate(history):
                if eid == after_event_id:
                    found = True
                    history = history[i + 1:]
                    break
            if not found:
                history = []

        result = []
        for eid, data in history[-limit:]:
            result.append({"event": "reconnect", "id": eid, "data": data})
        return result

    # ---- monitoring ----

    async def get_stats(self) -> Dict[str, Any]:
        """Return connection statistics for monitoring."""
        async with self._lock:
            return {
                "total_connections": sum(len(qs) for qs in self._connections.values()),
                "active_tasks": len(self._connections),
                "history_sizes": {k: len(v) for k, v in self._history.items()},
            }


# Module-level singleton
_manager: Optional[ConnectionManager] = None


def get_connection_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


def reset_connection_manager() -> None:
    global _manager
    _manager = None
