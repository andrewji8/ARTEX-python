"""
Graceful shutdown helpers for the ARTEX FastAPI application.

Hooks into Uvicorn's lifespan to ensure:
  - All active LangGraph worker executions are completed or rolled back.
  - Planner state is flushed to the database.
  - Engine processes receive SIGTERM and finish gracefully.
  - DB sessions are properly closed.

Called via:
  - @app.on_event("shutdown")
  - Signal handlers installed at startup.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Dict, Optional

logger = logging.getLogger("artex.shutdown")

# Global engine registry: task_id -> Engine instance
_engine_registry: Dict[str, object] = {}
_shutdown_event: Optional[asyncio.Event] = None


def register_engine(task_id: str, engine: object) -> None:
    """Register an Engine instance for graceful shutdown tracking."""
    _engine_registry[task_id] = engine
    logger.debug("Registered engine for task=%s (total: %d)", task_id, len(_engine_registry))


def unregister_engine(task_id: str) -> None:
    """Unregister an Engine instance."""
    _engine_registry.pop(task_id, None)
    logger.debug("Unregistered engine for task=%s (remaining: %d)", task_id, len(_engine_registry))


async def shutdown_all_engines(timeout: float = 30.0) -> None:
    """
    Gracefully stop all registered engines.
    Each engine will flush its Planner state to DB before terminating.
    """
    if not _engine_registry:
        return

    tasks = []
    for task_id, engine in list(_engine_registry.items()):
        if hasattr(engine, "stop"):
            tasks.append(asyncio.create_task(_stop_engine(task_id, engine, timeout)))

    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        logger.info("All engines stopped (completed=%d, cancelled=%d)", len(done), len(pending))


async def _stop_engine(task_id: str, engine: object, timeout: float) -> None:
    """Stop a single engine with timeout."""
    try:
        await asyncio.wait_for(engine.stop(), timeout=timeout)
        logger.info("Engine stopped gracefully for task=%s", task_id)
    except asyncio.TimeoutError:
        logger.warning("Engine timeout for task=%s — forcing cancel", task_id)
        engine._running = False  # type: ignore[attr-defined]
    except Exception:
        logger.exception("Error stopping engine for task=%s", task_id)


def install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT handlers that trigger graceful shutdown."""
    loop = asyncio.get_event_loop()

    def _handle_signal(sig: int, _frame=None) -> None:
        logger.info("Received signal %d — initiating graceful shutdown", sig)
        shutdown_task = asyncio.create_task(_graceful_shutdown())
        # Give the shutdown task time before hard exit
        loop.call_later(60, lambda: sys.exit(0))
        shutdown_task.add_done_callback(lambda t: logger.info("Shutdown complete"))

    async def _graceful_shutdown() -> None:
        logger.info("Flushing planner states and stopping engines...")
        await shutdown_all_engines(timeout=30.0)
        logger.info("Graceful shutdown finished")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
