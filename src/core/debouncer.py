"""Debouncer that coalesces graph-change events into a single Planner wake-up."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Optional
from uuid import UUID


class Debouncer:
    """
    Coalesces rapid-fire events into a single callback invocation.
    Each new event resets the delay timer.  Only when the window expires
    without a fresh event does the callback fire once.

    Usage:
        debouncer = Debouncer(delay=2.0, callback=on_planner_wake)
        await debouncer.notify(task_id, "new fact")   # resets timer
        ...
        await debouncer.stop()
    """

    def __init__(
        self,
        *,
        delay: float = 2.0,
        callback: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._delay = delay
        self._callback = callback
        self._lock = asyncio.Lock()
        self._timer: Optional[asyncio.TimerHandle] = None
        self._pending_payload: Dict[str, Any] = {}
        self._stopped = False

    async def notify(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Register a notification; resets the debounce window."""
        if self._stopped:
            return
        payload = payload or {}
        async with self._lock:
            self._pending_payload = {**self._pending_payload, **payload}
            self._cancel_timer()
            self._timer = asyncio.get_event_loop().call_later(
                self._delay,
                lambda: asyncio.create_task(self._fire(task_id)),
            )

    async def _fire(self, task_id: str) -> None:
        async with self._lock:
            if self._stopped:
                return
            payload = self._pending_payload
            self._pending_payload = {}
        await self._callback(task_id, payload)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def stop(self) -> None:
        self._stopped = True
        self._cancel_timer()

    @property
    def pending(self) -> Dict[str, Any]:
        return dict(self._pending_payload)
