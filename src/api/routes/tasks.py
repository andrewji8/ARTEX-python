"""Task routes: REST endpoints + SSE Activity Stream."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, AsyncGenerator, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from src.api.dependencies import get_current_active_user, User
from src.api.stream_manager import ConnectionManager, get_connection_manager
from src.db.repository import AsyncGraphRepository
from src.db.schemas import AnchorCreate, ExplorationNodeCreate, TaskCreate, TaskUpdate, TaskStatusEnum
from src.db.session import session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Helper: DB session from request state (set by lifespan)
# ---------------------------------------------------------------------------

def _get_repo(request: Request) -> AsyncGraphRepository:
    """Extract repo from the request's ASGI lifespan-scoped state."""
    return request.state.repo  # type: ignore[attr-defined]


def _get_manager(request: Request) -> ConnectionManager:
    return request.state.connection_manager  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SSE Activity Stream
# ---------------------------------------------------------------------------

@router.get("/{task_id}/stream")
async def task_activity_stream(
    task_id: UUID,
    request: Request,
    last_event_id: Annotated[str, Query(description="Last-Event-ID for reconnection")] = "",
    user: User = Depends(get_current_active_user),
):
    """
    SSE endpoint for real-time activity stream.
    Connects to the EventBus and forwards engine events as SSE events.

    SSE event format:
        event: <event_type>
        id: <event_id>
        data: <json_payload>

    Supports Last-Event-ID reconnection.
    """
    manager = _get_manager(request)
    queue = await manager.connect(task_id)

    async def _event_generator() -> AsyncGenerator[dict, None]:
        try:
            # Reconnection: replay missed events
            if last_event_id:
                history = await manager.get_history(task_id, after_event_id=last_event_id, limit=50)
                for item in history:
                    yield {
                        "event": item["event"],
                        "data": json.dumps(item["data"], default=str),
                        "id": item["id"],
                    }

            # Main loop: stream new events
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield {"event": "heartbeat", "data": "{}", "id": "hb"}
                    continue

                yield {
                    "event": event["event"],
                    "data": json.dumps(event["data"], default=str),
                    "id": event["id"],
                }
        except asyncio.CancelledError:
            logger.info("SSE stream cancelled for task_id=%s", task_id)
        finally:
            await manager.disconnect(task_id, queue)

    import asyncio
    return EventSourceResponse(_event_generator(), sep="\n")


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def list_tasks(request: Request,
    company_id: Optional[UUID] = Query(None, description="Filter by company"),
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_active_user),
):
    """List tasks with optional filtering."""
    repo = _get_repo(request)
    # Use raw session for flexible querying
    from sqlalchemy import select, func
    from src.db.models import Task

    stmt = select(Task)
    conditions = []
    if company_id:
        conditions.append(Task.company_id == company_id)
    if status_filter:
        conditions.append(Task.status == status_filter)
    if conditions:
        stmt = stmt.where(*conditions)

    count_stmt = select(func.count()).select_from(Task).where(*conditions) if conditions else select(func.count()).select_from(Task)
    total = (await repo.db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Task.created_at.desc()).offset(skip).limit(limit)
    result = await repo.db.execute(stmt)
    tasks = result.scalars().all()

    return {
        "items": [
            {
                "id": str(t.id),
                "company_id": str(t.company_id),
                "task_type": t.task_type,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "current_iteration": t.current_iteration,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tasks
        ],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/{task_id}")
async def get_task(request: Request,
    task_id: UUID,
    user: User = Depends(get_current_active_user),
):
    """Get a single task by ID."""
    repo = _get_repo(request)
    task_out = await repo.get_task(task_id)
    if not task_out:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task_out


@router.get("/{task_id}/context")
async def get_task_context(request: Request,
    task_id: UUID,
    include_activities: bool = Query(True, description="Include activity stream in context"),
    user: User = Depends(get_current_active_user),
):
    """
    Return the full task context: task metadata + exploration graph (nodes, anchors)
    + recent activities. Optimized single-query load via DAL.
    """
    repo = _get_repo(request)
    context = await repo.get_task_context(task_id)
    if not context:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Optionally strip activities to reduce payload size
    if not include_activities:
        context.activities = []
        context.total_activities = 0

    return context


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_task(request: Request,
    payload: TaskCreate,
    user: User = Depends(get_current_active_user),
):
    """Create a new penetration testing task."""
    repo = _get_repo(request)
    task = await repo.create_task(
        company_id=payload.company_id,
        title=payload.title,
        task_type=payload.task_type.value,
        description=payload.description,
        priority=payload.priority,
        max_iterations=payload.max_iterations,
        agent_config=payload.agent_config,
        context_data=payload.context_data,
        asset_id=payload.asset_id,
    )
    return task


@router.patch("/{task_id}")
async def update_task(request: Request,
    task_id: UUID,
    payload: TaskUpdate,
    user: User = Depends(get_current_active_user),
):
    """Update task metadata or status."""
    repo = _get_repo(request)
    existing = await repo.get_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if payload.status:
        await repo.update_task_status(task_id, payload.status.value)

    task = await repo.get_task(task_id)
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(request: Request,
    task_id: UUID,
    user: User = Depends(get_current_active_user),
):
    """Delete a task and all its associated data (cascade)."""
    repo = _get_repo(request)
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from sqlalchemy import delete, select
    from src.db.models import Task, ExplorationNode, ExplorationAnchor, Activity, Intent

    async with repo.db.begin() as txn:
        await txn.execute(delete(Activity).where(Activity.task_id == task_id))
        await txn.execute(delete(ExplorationAnchor).where(
            ExplorationAnchor.from_node_id.in_(
                select(ExplorationNode.id).where(ExplorationNode.task_id == task_id)
            )
        ))
        await txn.execute(delete(ExplorationNode).where(ExplorationNode.task_id == task_id))
        await txn.execute(delete(Intent).where(Intent.task_id == task_id))
        await txn.execute(delete(Task).where(Task.id == task_id))

    return JSONResponse(status_code=204)


@router.post("/{task_id}/intents", status_code=status.HTTP_201_CREATED)
async def inject_intent(request: Request,
    task_id: UUID,
    payload: dict[str, Any],
    user: User = Depends(get_current_active_user),
):
    """
    Human-in-the-loop: manually inject an intent into the task's todolist.
    Accepts: title (required), description, node_type, prerequisite_node_ids, priority.
    """
    repo = _get_repo(request)

    # Verify task exists
    task = await repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    title = payload.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    intent = await repo.create_intent(
        task_id=task_id,
        title=title,
        description=payload.get("description", ""),
        intent_type=payload.get("node_type", "investigation"),
        prerequisite_node_ids=[UUID(fid) for fid in (payload.get("prerequisite_node_ids") or [])],
        priority=payload.get("priority", 0),
        extra_data=payload.get("extra_data", {}),
    )

    # Notify SSE subscribers about the new intent
    manager = _get_manager(request)
    await manager.broadcast(
        task_id,
        {
            "type": "intent_injected",
            "intent_id": str(intent.id),
            "title": intent.title,
            "priority": intent.priority,
        },
        event_type="intent",
    )

    return {
        "id": str(intent.id),
        "task_id": str(intent.task_id),
        "title": intent.title,
        "status": intent.status,
        "priority": intent.priority,
        "created_at": intent.created_at.isoformat() if intent.created_at else None,
    }


@router.post("/{task_id}/nodes", status_code=status.HTTP_201_CREATED)
async def create_exploration_node(request: Request,
    task_id: UUID,
    payload: ExplorationNodeCreate,
    user: User = Depends(get_current_active_user),
):
    """Manually create an exploration node (for HITL graph manipulation)."""
    repo = _get_repo(request)
    node = await repo.create_node(
        task_id=task_id,
        node_type=payload.node_type.value,
        title=payload.title,
        content=payload.content,
        confidence=payload.confidence,
        source=payload.source,
        tool_name=payload.tool_name,
        extra_data=payload.extra_data,
        is_verified=payload.is_verified,
        depth_level=payload.depth_level,
    )

    # Broadcast node creation to SSE
    manager = _get_manager(request)
    await manager.broadcast(
        task_id,
        {"type": "node_created", "node_id": str(node.id), "node_type": node.node_type, "title": node.title},
        event_type="node",
    )

    return {
        "id": str(node.id),
        "task_id": str(node.task_id),
        "node_type": node.node_type,
        "title": node.title,
        "confidence": node.confidence,
        "created_at": node.created_at.isoformat() if node.created_at else None,
    }


@router.post("/{task_id}/anchors", status_code=status.HTTP_201_CREATED)
async def create_anchor(request: Request,
    task_id: UUID,
    payload: AnchorCreate,
    user: User = Depends(get_current_active_user),
):
    """Create an anchor (edge) between two exploration nodes."""
    repo = _get_repo(request)
    anchor = await repo.create_anchor(payload)

    manager = _get_manager(request)
    await manager.broadcast(
        task_id,
        {
            "type": "anchor_created",
            "anchor_id": str(anchor.id),
            "from_node_id": str(anchor.from_node_id),
            "to_node_id": str(anchor.to_node_id),
            "anchor_type": anchor.anchor_type,
        },
        event_type="anchor",
    )

    return {
        "id": str(anchor.id),
        "from_node_id": str(anchor.from_node_id),
        "to_node_id": str(anchor.to_node_id),
        "anchor_type": anchor.anchor_type,
        "weight": anchor.weight,
    }


@router.get("/{task_id}/intents")
async def list_task_intents(request: Request,
    task_id: UUID,
    status_filter: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_active_user),
):
    """List intents for a task with optional status filter."""
    repo = _get_repo(request)

    from sqlalchemy import select, func
    from src.db.models import Intent

    conditions = [Intent.task_id == task_id]
    if status_filter:
        conditions.append(Intent.status == status_filter)

    count_stmt = select(func.count()).select_from(Intent).where(*conditions)
    total = (await repo.db.execute(count_stmt)).scalar_one()

    stmt = (
        select(Intent)
        .where(*conditions)
        .order_by(Intent.priority.desc(), Intent.created_at.asc())
        .offset(skip)
        .limit(limit)
    )
    result = await repo.db.execute(stmt)
    intents = result.scalars().all()

    return {
        "items": [
            {
                "id": str(i.id),
                "task_id": str(i.task_id),
                "intent_type": i.intent_type,
                "title": i.title,
                "description": i.description,
                "status": i.status,
                "priority": i.priority,
                "claimed_by_worker_id": i.claimed_by_worker_id,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in intents
        ],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/stream")
async def global_activity_stream(
    request: Request,
    user: User = Depends(get_current_active_user),
):
    """
    Global SSE stream of the most recent activities across all tasks.
    Polls the DB periodically and pushes new activities as SSE events.
    (A lightweight alternative to per-task streams for the dashboard feed.)
    """
    import asyncio
    import json
    from sqlalchemy import select
    from src.db.models import Activity
    from sse_starlette.sse import EventSourceResponse

    repo = _get_repo(request)
    seen = set()

    async def _gen():
        # Seed with the latest activity id so we only push *new* ones.
        stmt = select(Activity.id).order_by(Activity.created_at.desc()).limit(1)
        row = (await repo.db.execute(stmt)).scalar_one_or_none()
        if row:
            seen.add(str(row))
        while True:
            await asyncio.sleep(2.0)
            try:
                stmt = (
                    select(Activity)
                    .order_by(Activity.created_at.desc())
                    .limit(20)
                )
                result = await repo.db.execute(stmt)
                activities = result.scalars().all()
                for a in reversed(activities):
                    aid = str(a.id)
                    if aid in seen:
                        continue
                    seen.add(aid)
                    if len(seen) > 500:
                        seen.clear()
                    yield {
                        "event": a.activity_type,
                        "data": json.dumps({
                            "type": "activity",
                            "activity_type": a.activity_type,
                            "agent_thought": a.agent_thought,
                            "tool": a.tool_name,
                            "ts": a.created_at.isoformat() if a.created_at else None,
                        }, default=str),
                        "id": aid,
                    }
            except Exception as exc:  # noqa: BLE001
                logger.debug("global stream poll error: %s", exc)
                continue

    return EventSourceResponse(_gen(), sep="\n")


@router.get("/{task_id}/stats")
async def get_task_stats(request: Request,
    task_id: UUID,
    user: User = Depends(get_current_active_user),
):
    """Return task statistics: intent counts by status, connection stats."""
    repo = _get_repo(request)
    manager = _get_manager(request)

    intent_counts = await repo.count_by_status(task_id)
    conn_stats = await manager.get_stats()

    return {
        "task_id": str(task_id),
        "intent_counts": intent_counts,
        "sse_connections": conn_stats.get("active_tasks", 0),
    }
