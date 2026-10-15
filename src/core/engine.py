"""ARTEX Event-Driven Engine — ties EventBus, Planner, and WorkerPool together."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from src.core.debouncer import Debouncer
from src.core.event_bus import EventBus, Event, EventType
from src.core.planner import build_planner_graph
from src.core.planner_state import IntentStep, PlannerState
from src.core.worker_pool import WorkerPool
from src.tools.real_tools import ToolExecutor
from src.db.repository import AsyncGraphRepository

logger = logging.getLogger(__name__)


class Engine:
    """
    Central orchestrator for the ARTEX event-driven loop:

        Graph Changes (Debounce)
          -> Planner wakes up
            -> Planner spawns Intents into Shared Todolist
              -> Workers claim Intents
                -> Execute Tools
                  -> Update Graph
                    -> Trigger next loop
    """

    def __init__(
        self,
        *,
        task_id: UUID,
        repo: AsyncGraphRepository,
        event_bus: EventBus,
        num_workers: int = 4,
        debounce_delay: float = 2.0,
        llm_pool: Optional[Any] = None,
        tool_executor: Optional[ToolExecutor] = None,
    ) -> None:
        self.task_id = task_id
        self.repo = repo
        self.event_bus = event_bus
        self.num_workers = num_workers
        self._debounce_delay = debounce_delay
        self.llm_pool = llm_pool
        self.tool_executor = tool_executor

        self._planner_graph: Any = None
        self._worker_pool: Optional[WorkerPool] = None
        self._debouncer: Optional[Debouncer] = None
        self._running = False
        self._planner_task: Optional[asyncio.Task] = None
        self._cycle_count = 0

    # ---- lifecycle ----

    async def start(self) -> None:
        """Start the engine: build graphs, spawn workers, begin event loop."""
        self._running = True
        self._planner_graph = build_planner_graph(self.repo)

        # Build a worker graph factory bound to this engine's repo
        from src.core.worker import build_worker_graph
        worker_graph = build_worker_graph(
            self.repo, self, llm_pool=self.llm_pool, tool_executor=self.tool_executor,
        )

        self._debouncer = Debouncer(
            delay=self._debounce_delay,
            callback=self._on_planner_wake,
        )

        self._worker_pool = WorkerPool(
            num_workers=self.num_workers,
            repo=self.repo,
            event_bus=self.event_bus,
            debounce_notify=self._debouncer.notify,
            task_id=self.task_id,
        )
        await self._worker_pool.start(self._planner_graph, lambda: worker_graph)

        await self.event_bus.start()
        await self._subscribe_to_events()

        # Kick off initial planner run
        self._planner_task = asyncio.create_task(self._run_loop())
        logger.info("Engine started for task_id=%s", self.task_id)

    async def stop(self) -> None:
        """Gracefully shut down the engine."""
        self._running = False
        if self._debouncer:
            await self._debouncer.stop()
        if self._worker_pool:
            await self._worker_pool.stop()
        if self._planner_task:
            self._planner_task.cancel()
            try:
                await self._planner_task
            except asyncio.CancelledError:
                pass
        await self.event_bus.stop()
        logger.info("Engine stopped for task_id=%s", self.task_id)

    # ---- event subscription ----

    async def _subscribe_to_events(self) -> None:
        """Register the debouncer as subscriber to all graph-change events."""
        async def _on_event(event: Event) -> None:
            if event.task_id != self.task_id:
                return
            await self._debouncer.notify(
                str(self.task_id),
                {"event_type": event.event_type.value, "payload": event.payload},
            )

        for etype in [
            EventType.NODE_CREATED,
            EventType.NODE_UPDATED,
            EventType.ANCHOR_CREATED,
            EventType.ACTIVITY_APPENDED,
        ]:
            await self.event_bus.subscribe(_on_event, task_id=self.task_id, event_type=etype)

        # Also listen to manual wake events directly (bypass debounce)
        async def _on_manual_wake(event: Event) -> None:
            if event.task_id == self.task_id:
                asyncio.create_task(self._on_planner_wake(str(self.task_id), event.payload))

        await self.event_bus.subscribe(_on_manual_wake, task_id=self.task_id, event_type=EventType.MANUAL_WAKE)

    # ---- planner wake callback ----

    async def _on_planner_wake(self, _task_id: str, payload: Dict[str, Any]) -> None:
        """Called by Debouncer after the debounce window expires."""
        logger.debug("Planner woken by reason=%s", payload.get("event_type", "manual"))
        try:
            await self._run_planner_cycle()
        except Exception:
            logger.exception("Planner cycle failed")

    # ---- main loop ----

    async def _run_loop(self) -> None:
        """Continuous loop: plan -> distribute -> wait for events."""
        while self._running:
            await self._run_planner_cycle()
            # Wait until next event (non-blocking poll)
            event = await self.event_bus.get_event(self.task_id, timeout=1.0)
            if event and event.event_type == EventType.MANUAL_WAKE:
                asyncio.create_task(self._on_planner_wake(str(self.task_id), event.payload))

    async def _run_planner_cycle(self) -> None:
        """Run one Planner cycle: load persisted state, plan, persist state, create DB intents."""
        # Fix #2: load persisted PlannerState from DB
        saved_state = await self.repo.load_planner_state(self.task_id)
        initial_state = {
            "task_id": str(self.task_id),
            "current_graph_summary": saved_state.get("current_graph_summary", {}),
            "todolist": [IntentStep(**s) for s in saved_state.get("todolist", [])],
            "frontier": [IntentStep(**s) for s in saved_state.get("frontier", [])],
            "last_wake_reason": saved_state.get("last_wake_reason", "initial" if self._cycle_count == 0 else "graph_change"),
            "cycle_count": self._cycle_count,
            "completed_intent_ids": set(saved_state.get("completed_intent_ids", [])),
            "failed_intent_ids": set(saved_state.get("failed_intent_ids", [])),
            "metadata": saved_state.get("metadata", {}),
        }

        result = await self._planner_graph.ainvoke(initial_state)
        self._cycle_count += 1

        # Fix #2: persist PlannerState back to DB
        output_state: Dict[str, Any] = {
            "current_graph_summary": result.get("current_graph_summary", {}),
            "todolist": [s.model_dump() for s in (result.get("todolist") or [])],
            "frontier": [s.model_dump() for s in (result.get("frontier") or [])],
            "last_wake_reason": result.get("last_wake_reason", ""),
            "cycle_count": result.get("cycle_count", self._cycle_count),
            "completed_intent_ids": list(result.get("completed_intent_ids", set())),
            "failed_intent_ids": list(result.get("failed_intent_ids", set())),
            "metadata": result.get("metadata", {}),
        }
        await self.repo.save_planner_state(self.task_id, output_state)

        # Fix #1: create DB intents from frontier instead of in-memory todolist
        frontier: List[IntentStep] = result.get("frontier", []) or []
        if frontier:
            intents_data = [
                {
                    "title": step.title,
                    "description": step.description,
                    "intent_type": step.node_type,
                    "prerequisite_node_ids": [str(fid) for fid in step.prerequisite_fact_ids],
                    "priority": step.priority,
                    "extra_data": {},
                }
                for step in frontier
            ]
            created = await self.repo.batch_create_intents(self.task_id, intents_data)
            logger.info(
                "Planner cycle #%d: created %d intents, frontier=%d",
                self._cycle_count, len(created), len(frontier),
            )
        else:
            logger.debug("Planner cycle #%d: no new frontier intents", self._cycle_count)

        # Persist planner state + any new intents (engine session is autocommit=False).
        if getattr(self.repo, "db", None) is not None:
            try:
                await self.repo.db.commit()
            except Exception:
                await self.repo.db.rollback()

    # ---- public API ----

    async def wake_planner(self) -> None:
        """Manually trigger a planner wake-up (bypasses debounce)."""
        await self.event_bus.publish(Event(
            event_type=EventType.MANUAL_WAKE,
            task_id=self.task_id,
            payload={"reason": "manual_wake"},
        ))

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def worker_pool(self) -> Optional[WorkerPool]:
        return self._worker_pool


    async def mark_intent_completed(
        self, task_id: UUID, intent_id: UUID, worker_id: str
    ) -> None:
        """No-op hook for workers.

        The authoritative DB completion is performed by
        ``WorkerPool._worker_loop`` via ``repo.complete_intent``. This method
        exists so the worker graph can signal the pool when the pool binding
        is the Engine itself (as wired in ``Engine.start``).
        """
        return

