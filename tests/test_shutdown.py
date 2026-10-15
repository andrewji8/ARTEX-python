"""Tests for the graceful-shutdown engine registry (src/api/shutdown.py)."""

from __future__ import annotations

import asyncio

import pytest

from src.api.shutdown import (
    register_engine,
    shutdown_all_engines,
    unregister_engine,
    _engine_registry,
)


class FakeEngine:
    def __init__(self, fail=False, delay=0.0):
        self.stopped = False
        self.fail = fail
        self.delay = delay
        self._running = True

    async def stop(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("stop failed")
        self.stopped = True
        self._running = False


class TestEngineRegistry:
    def setup_method(self):
        _engine_registry.clear()

    def teardown_method(self):
        _engine_registry.clear()

    def test_register_unregister(self):
        eng = FakeEngine()
        register_engine("task-1", eng)
        assert "task-1" in _engine_registry
        unregister_engine("task-1")
        assert "task-1" not in _engine_registry

    def test_unregister_missing_is_noop(self):
        unregister_engine("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_shutdown_all_stops_engines(self):
        e1, e2 = FakeEngine(), FakeEngine()
        register_engine("t1", e1)
        register_engine("t2", e2)
        await shutdown_all_engines(timeout=5.0)
        assert e1.stopped and e2.stopped

    @pytest.mark.asyncio
    async def test_shutdown_empty_registry_noop(self):
        await shutdown_all_engines(timeout=1.0)  # no engines -> returns fast
        assert _engine_registry == {}

    @pytest.mark.asyncio
    async def test_shutdown_survives_engine_error(self):
        good = FakeEngine()
        bad = FakeEngine(fail=True)
        register_engine("good", good)
        register_engine("bad", bad)
        await shutdown_all_engines(timeout=5.0)  # should not raise
        assert good.stopped
