"""Event bus with async debounce for the ARTEX event-driven engine."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from uuid import UUID, uuid4


class EventType(str, Enum):
    NODE_CREATED = "node_created"
    NODE_UPDATED = "node_updated"
    ANCHOR_CREATED = "anchor_created"
    ACTIVITY_APPENDED = "activity_appended"
    TASK_STATUS_CHANGED = "task_status_changed"
    INTENT_CLAIMED = "intent_claimed"
    INTENT_COMPLETED = "intent_completed"
    INTENT_FAILED = "intent_failed"
    MANUAL_WAKE = "manual_wake"


@dataclass
class Event:
    event_id: UUID = field(default_factory=uuid4)
    event_type: EventType = EventType.MANUAL_WAKE
    task_id: Optional[UUID] = None
    node_id: Optional[UUID] = None
    actor_worker_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __hash__(self) -> int:
        return hash(self.event_id)


class EventBus:
    """
    Thread-safe (async-safe) event bus using asyncio.Queue.
    Broadcasts events to registered callbacks per (task_id, event_type).
    """

    def __init__(self, maxsize: int = 4096) -> None:
        self._queues: Dict[UUID, asyncio.Queue[Event]] = {}
        self._subscribers: Dict[UUID, Set[Callable[[Event], Coroutine[Any, Any, None]]]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._running = False
        self._maxsize = maxsize
        self._dispatch_tasks: List[asyncio.Task] = []

    # ---- registration ----

    async def subscribe(
        self,
        callback: Callable[[Event], Coroutine[Any, Any, None]],
        task_id: Optional[UUID] = None,
        event_type: Optional[EventType] = None,
    ) -> None:
        async with self._lock:
            if task_id not in self._subscribers:
                self._subscribers[task_id] = set()
            self._subscribers[task_id].add(callback)
            if event_type is not None:
                if task_id not in self._queues:
                    self._queues[task_id] = asyncio.Queue(maxsize=self._maxsize)

    async def unsubscribe(
        self,
        callback: Callable[[Event], Coroutine[Any, Any, None]],
        task_id: Optional[UUID] = None,
    ) -> None:
        async with self._lock:
            if task_id and task_id in self._subscribers:
                self._subscribers[task_id].discard(callback)

    # ---- publish ----

    async def publish(self, event: Event) -> None:
        """Broadcast an event to all matching subscribers."""
        async with self._lock:
            subscribers = list(self._subscribers.get(event.task_id, set()))
            if not subscribers:
                return

        for cb in subscribers:
            try:
                await cb(event)
            except Exception:
                pass  # one failing subscriber must not block others

    async def publish_to_queue(self, event: Event) -> None:
        """Publish to a specific async queue (for consumers that poll)."""
        if event.task_id not in self._queues:
            self._queues[event.task_id] = asyncio.Queue(maxsize=self._maxsize)
        await self._queues[event.task_id].put(event)

    async def get_event(self, task_id: UUID, timeout: Optional[float] = None) -> Optional[Event]:
        """Poll an event from the per-task queue."""
        q = self._queues.get(task_id)
        if q is None:
            return None
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ---- lifecycle ----

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for t in self._dispatch_tasks:
            t.cancel()
        self._dispatch_tasks.clear()

    @property
    def running(self) -> bool:
        return self._running
