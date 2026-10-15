"""Tests for the WorkerPool (src/core/worker_pool.py) with a fake repository."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List
from uuid import UUID, uuid4

import pytest

from src.core.worker_pool import WorkerPool


@dataclass
class FakeIntent:
    id: UUID
    task_id: UUID
    title: str = "intent"
    description: str = "desc"
    intent_type: str = "intent"
    priority: int = 50


class FakeRepo:
    """A repo that hands out a fixed queue of intents, then None."""

    def __init__(self, intents: List[FakeIntent]):
        self._queue = list(intents)
        self.claimed: List[tuple] = []
        self.completed: List[UUID] = []
        self.failed: List[UUID] = []

    async def claim_intent(self, task_id, worker_id):
        if not self._queue:
            return None
        intent = self._queue.pop(0)
        self.claimed.append((intent.id, worker_id))
        return intent

    async def complete_intent(self, intent_id, *, result_node_id=None, error_message=None):
        self.completed.append(intent_id)
        return None

    async def fail_intent(self, intent_id, *, error_message):
        self.failed.append(intent_id)
        return None

    async def release_intent(self, intent_id):
        return None

    async def count_by_status(self, task_id):
        return {"pending": 0, "claimed": 0, "completed": len(self.completed), "failed": len(self.failed)}


def _pool(repo, num_workers=1):
    return WorkerPool(num_workers=num_workers, repo=repo, event_bus=None, debounce_notify=None)


class TestWorkerPoolLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        repo = FakeRepo([])
        pool = _pool(repo, num_workers=2)
        await pool.start(None, lambda: None)
        assert pool._running
        await asyncio.sleep(0.05)
        await pool.stop()
        assert not pool._running

    @pytest.mark.asyncio
    async def test_claims_and_completes_intent(self):
        # A worker graph factory whose graph.invoke returns a result node id.
        task_id = uuid4()
        intent = FakeIntent(id=uuid4(), task_id=task_id)
        repo = FakeRepo([intent])

        class FakeGraph:
            async def ainvoke(self, state):
                return {"result_node_id": None, "status": "done"}

        pool = _pool(repo, num_workers=1)
        await pool.start(None, lambda: FakeGraph())
        # Give the worker loop time to claim + complete.
        for _ in range(30):
            if repo.completed:
                break
            await asyncio.sleep(0.02)
        await pool.stop()

        assert len(repo.claimed) >= 1
        assert intent.id in repo.completed

    @pytest.mark.asyncio
    async def test_get_status_summary(self):
        task_id = uuid4()
        repo = FakeRepo([])
        repo.completed.append(uuid4())
        pool = _pool(repo)
        summary = await pool.get_status_summary(task_id)
        assert summary["completed"] == 1
        assert summary["pending"] == 0
