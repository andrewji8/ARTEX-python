"""Worker pool — manages N async workers with DB-backed atomic intent claiming."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from src.core.worker_state import WorkerState
from src.db.models import Intent, IntentStatus
from src.db.repository import AsyncGraphRepository

logger = logging.getLogger(__name__)


class WorkerPool:
    """
    Manages N async workers that pull intents from the DB via
    SELECT ... FOR UPDATE SKIP LOCKED — distributed-safe across processes.
    """

    def __init__(
        self,
        *,
        num_workers: int = 4,
        repo: AsyncGraphRepository,
        event_bus: Any,
        debounce_notify: Any,
        task_id: Optional[UUID] = None,
    ) -> None:
        self.num_workers = num_workers
        self.repo = repo
        self.event_bus = event_bus
        self._debounce_notify = debounce_notify
        self.task_id = task_id
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._worker_states: Dict[str, WorkerState] = {}

    # ---- monitoring ----

    async def get_status_summary(self, task_id: UUID) -> Dict[str, Any]:
        """Return intent counts by status for the task."""
        counts = await self.repo.count_by_status(task_id)
        return {
            "pending": counts.get("pending", 0),
            "claimed": counts.get("claimed", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
        }

    # ---- lifecycle ----

    async def start(self, planner_graph: Any, worker_graph_factory: Any) -> None:
        self._running = True
        for i in range(self.num_workers):
            worker_id = f"worker-{i}"
            task = asyncio.create_task(
                self._worker_loop(worker_id, planner_graph, worker_graph_factory),
                name=f"worker-{i}",
            )
            self._tasks.append(task)
        logger.info("WorkerPool started with %d workers", self.num_workers)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("WorkerPool stopped")

    async def _worker_loop(
        self,
        worker_id: str,
        planner_graph: Any,
        worker_graph_factory: Any,
    ) -> None:
        """Main loop: atomic DB claim → execute → release/complete."""
        claim_task_id = self.task_id if self.task_id is not None else UUID(int=0)
        state = WorkerState(task_id=claim_task_id, worker_id=worker_id, status="idle")
        self._worker_states[worker_id] = state

        while self._running:
            # Sleep briefly when idle to avoid busy-looping
            await asyncio.sleep(0.1)

            # Try to claim an intent atomically from DB
            # For a per-task pool, always claim against this task's id so the
            # production `claim_intent` (which filters Intent.task_id) matches.
            intent: Optional[Intent] = await self.repo.claim_intent(
                claim_task_id, worker_id
            )
            if intent is None:
                continue
            # Make the claim durable (session is autocommit=False / no autoflush).
            if getattr(self.repo, "db", None) is not None:
                await self.repo.db.commit()

            state = WorkerState(
                task_id=intent.task_id,
                worker_id=worker_id,
                intent={
                    "intent_id": str(intent.id),
                    "title": intent.title,
                    "description": intent.description or "",
                    "node_type": intent.intent_type,
                    "priority": intent.priority,
                    "task_id": str(intent.task_id),
                    "status": "claimed",
                },
                status="working",
            )
            self._worker_states[worker_id] = state

            try:
                worker_graph = worker_graph_factory()
                result = await worker_graph.ainvoke(state.model_dump())
                # Mark completed in DB
                _rni = result.get("result_node_id")
                if isinstance(_rni, UUID):
                    result_node_id = _rni
                elif _rni:
                    result_node_id = UUID(_rni)
                else:
                    result_node_id = None
                await self.repo.complete_intent(intent.id, result_node_id=result_node_id)

                # Persist worker-produced activities to the DB
                for act in (result.get("activities") or []):
                    try:
                        # NOTE: activities.intent_id is an FK to exploration_nodes.id,
                        # not to the intents table. Carry the intent correlation in
                        # metadata_json instead of the FK column.
                        meta = dict(act.extra or {})
                        meta["intent_id"] = str(intent.id)
                        await self.repo.create_activity(
                            task_id=state.task_id,
                            intent_id=None,
                            activity_type=act.activity_type,
                            tool_name=act.tool_name,
                            tool_input=act.tool_input,
                            tool_output=act.tool_output,
                            agent_thought=act.agent_thought,
                            result_data=act.result_data,
                            duration_ms=act.duration_ms,
                            metadata_json=meta,
                        )
                    except Exception as act_exc:
                        logger.warning("worker=%s failed to persist activity: %s", worker_id, act_exc)
                # Commit the intent completion + persisted activities.
                if getattr(self.repo, "db", None) is not None:
                    await self.repo.db.commit()
                state.status = "done"
                logger.debug("worker=%s completed intent=%s", worker_id, intent.id)
            except Exception as exc:
                logger.exception("worker=%s failed on intent=%s", worker_id, intent.id)
                state.status = "error"
                state.error = str(exc)
                await self.repo.fail_intent(intent.id, error_message=str(exc))
                # Release back so another worker can retry
                await self.repo.release_intent(intent.id)
                if getattr(self.repo, "db", None) is not None:
                    await self.repo.db.commit()

        self._worker_states.pop(worker_id, None)
