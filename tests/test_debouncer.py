"""Tests for the Debouncer (src/core/debouncer.py)."""

from __future__ import annotations

import asyncio

import pytest

from src.core.debouncer import Debouncer


class TestDebouncer:
    @pytest.mark.asyncio
    async def test_single_notify_fires_after_delay(self):
        fired = []

        async def cb(task_id, payload):
            fired.append((task_id, payload))

        deb = Debouncer(delay=0.05, callback=cb)
        await deb.notify("task-1", {"a": 1})
        assert fired == []  # not yet
        await asyncio.sleep(0.1)
        assert len(fired) == 1
        assert fired[0][0] == "task-1"
        await deb.stop()

    @pytest.mark.asyncio
    async def test_rapid_notifies_coalesce_into_one(self):
        fired = []

        async def cb(task_id, payload):
            fired.append(payload)

        deb = Debouncer(delay=0.1, callback=cb)
        for i in range(5):
            await deb.notify("task-1", {"n": i})
            await asyncio.sleep(0.01)  # each resets the window
        await asyncio.sleep(0.2)
        assert len(fired) == 1  # coalesced
        assert fired[0]["n"] == 4  # latest payload wins
        await deb.stop()

    @pytest.mark.asyncio
    async def test_payload_merges(self):
        fired = []

        async def cb(task_id, payload):
            fired.append(payload)

        deb = Debouncer(delay=0.05, callback=cb)
        await deb.notify("task-1", {"x": 1})
        await deb.notify("task-1", {"y": 2})
        await asyncio.sleep(0.12)
        assert fired[0] == {"x": 1, "y": 2}
        await deb.stop()

    @pytest.mark.asyncio
    async def test_stop_prevents_firing(self):
        fired = []

        async def cb(task_id, payload):
            fired.append(payload)

        deb = Debouncer(delay=0.1, callback=cb)
        await deb.notify("task-1", {"x": 1})
        await deb.stop()
        await asyncio.sleep(0.2)
        assert fired == []

    @pytest.mark.asyncio
    async def test_pending_property(self):
        async def cb(task_id, payload):
            pass

        deb = Debouncer(delay=0.1, callback=cb)
        await deb.notify("task-1", {"x": 1})
        assert deb.pending == {"x": 1}
        await deb.stop()
