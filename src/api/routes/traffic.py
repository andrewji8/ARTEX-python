"""SSE endpoint for ingesting traffic data from mitmproxy addon."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.dependencies import get_current_active_user, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


@router.post("/ingest")
async def ingest_traffic(
    flow_data: Dict[str, Any],
    request: Request,
    user: Annotated[User, Depends(get_current_active_user)],
):
    """
    Internal webhook: receives traffic metadata from mitmproxy addon.
    Stores in DB and broadcasts to SSE subscribers.
    """
    repo = request.state.repo
    manager = request.state.connection_manager

    task_id_str = flow_data.get("task_id")
    if not task_id_str:
        raise HTTPException(status_code=400, detail="task_id is required")

    task_id = UUID(task_id_str)

    # Broadcast to SSE subscribers
    await manager.broadcast(
        task_id,
        {
            "type": "traffic",
            "event": flow_data.get("event"),
            "url": flow_data.get("url") or flow_data.get("request_url"),
            "method": flow_data.get("method") or flow_data.get("request_method"),
            "status_code": flow_data.get("status_code"),
            "body_size": flow_data.get("body_size"),
            "body_hash": flow_data.get("body_hash"),
            "timestamp": flow_data.get("timestamp"),
        },
        event_type="traffic",
    )

    logger.debug("Traffic ingested: task_id=%s event=%s url=%s", task_id, flow_data.get("event"), flow_data.get("url"))
    return {"status": "accepted", "event": flow_data.get("event")}


@router.get("/{task_id}")
async def list_traffic(
    task_id: UUID,
    user: Annotated[User, Depends(get_current_active_user)],
    limit: int = 100,
):
    """List captured traffic for a task (from DB or recent cache)."""
    # TODO: Query traffic records from DB
    return {"task_id": str(task_id), "items": [], "total": 0}
