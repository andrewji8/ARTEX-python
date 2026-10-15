"""Tests for EventBus (src/core/event_bus.py)."""

from __future__ import annotations

import asyncio

import pytest

from src.core.event_bus import Event, EventBus, EventType


def _event(task_id, event_type=EventType.NODE_CREATED, payload=None):
    return Event(task_id=task_id, event_type=event_type, payload=payload or {})


class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_to_subscriber(self):
        bus = EventBus()
        received = []

        async def cb(event):
            received.append(event)

        tid = "task-1"
        await bus.subscribe(cb, task_id=tid, event_type=EventType.NODE_CREATED)
        await bus.publish(_event(tid, EventType.NODE_CREATED))
        assert len(received) == 1
        assert received[0].task_id == tid

    @pytest.mark.asyncio
    async def test_no_subscriber_no_error(self):
        bus = EventBus()
        # Should not raise even with no subscribers.
        await bus.publish(_event("unknown-task"))
        assert True

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = EventBus()
        received = []

        async def cb(event):
            received.append(event)

        tid = "task-1"
        await bus.subscribe(cb, task_id=tid, event_type=EventType.NODE_CREATED)
        await bus.unsubscribe(cb, task_id=tid)
        await bus.publish(_event(tid, EventType.NODE_CREATED))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_one_failing_subscriber_does_not_block_others(self):
        bus = EventBus()
        ok = []

        async def bad(event):
            raise RuntimeError("boom")

        async def good(event):
            ok.append(event)

        tid = "task-1"
        await bus.subscribe(bad, task_id=tid, event_type=EventType.NODE_CREATED)
        await bus.subscribe(good, task_id=tid, event_type=EventType.NODE_CREATED)
        await bus.publish(_event(tid, EventType.NODE_CREATED))
        assert len(ok) == 1

    @pytest.mark.asyncio
    async def test_queue_publish_and_get(self):
        bus = EventBus()
        tid = "task-1"
        await bus.publish_to_queue(_event(tid, EventType.NODE_CREATED))
        ev = await bus.get_event(tid, timeout=1.0)
        assert ev is not None
        assert ev.task_id == tid

    @pytest.mark.asyncio
    async def test_get_event_timeout_returns_none(self):
        bus = EventBus()
        ev = await bus.get_event("missing-task", timeout=0.05)
        assert ev is None

    @pytest.mark.asyncio
    async def test_lifecycle_running_flag(self):
        bus = EventBus()
        assert not bus.running
        await bus.start()
        assert bus.running
        await bus.stop()
        assert not bus.running

    def test_event_hash_by_id(self):
        e = _event("t")
        assert hash(e) == hash(e.event_id)
        e2 = _event("t")
        assert hash(e) != hash(e2)
