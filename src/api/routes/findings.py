"""Findings and Goals routes — vulnerabilities and task objectives."""

from __future__ import annotations

from typing import Annotated, Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.api.dependencies import get_current_active_user, User
from src.api.stream_manager import ConnectionManager, get_connection_manager
from src.db.repository import AsyncGraphRepository
from src.db.schemas import (
    FindingCreate,
    FindingOut,
    FindingUpdate,
    GoalCreate,
    GoalOut,
    GoalUpdate,
)

router = APIRouter(prefix="/api", tags=["findings", "goals"])


def _get_repo(request: Request) -> AsyncGraphRepository:
    return request.state.repo


def _get_manager(request: Request) -> ConnectionManager:
    return request.state.connection_manager


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

@router.get("/tasks/{task_id}/findings")
async def list_findings(
    task_id: UUID,
    request: Request,
    severity: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """List findings for a task, optionally filtered by severity/status."""
    repo = _get_repo(request)
    items, total = await repo.list_findings(task_id, severity=severity, status=status_filter, skip=skip, limit=limit)
    return {
        "items": [f.model_dump(mode="json") for f in items],
        "total": total,
        "by_severity": await repo.findings_by_severity(task_id),
    }


@router.post("/tasks/{task_id}/findings", status_code=status.HTTP_201_CREATED)
async def create_finding(
    task_id: UUID,
    payload: FindingCreate,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Register a new finding for a task and broadcast to SSE subscribers."""
    repo = _get_repo(request)
    finding = await repo.create_finding(task_id, payload)

    manager = _get_manager(request)
    await manager.broadcast(
        task_id,
        {
            "type": "finding_created",
            "finding_id": str(finding.id),
            "title": finding.title,
            "severity": finding.severity,
        },
        event_type="finding",
    )
    return finding.model_dump(mode="json")


@router.get("/findings/{finding_id}")
async def get_finding(
    finding_id: UUID,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    finding = await repo.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail=f"Finding {finding_id} not found")
    return finding.model_dump(mode="json")


@router.patch("/findings/{finding_id}")
async def update_finding(
    finding_id: UUID,
    payload: FindingUpdate,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    finding = await repo.update_finding(finding_id, payload)
    if not finding:
        raise HTTPException(status_code=404, detail=f"Finding {finding_id} not found")
    return finding.model_dump(mode="json")


@router.delete("/findings/{finding_id}")
async def delete_finding(
    finding_id: UUID,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    ok = await repo.delete_finding(finding_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Finding {finding_id} not found")
    return {"deleted": str(finding_id)}


# --------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------

@router.get("/tasks/{task_id}/goals")
async def list_goals(
    task_id: UUID,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    goals = await repo.list_goals(task_id)
    total, met = await repo.goal_counts(task_id)
    return {
        "items": [g.model_dump(mode="json") for g in goals],
        "goals_total": total,
        "goals_met": met,
    }


@router.post("/tasks/{task_id}/goals", status_code=status.HTTP_201_CREATED)
async def create_goal(
    task_id: UUID,
    payload: GoalCreate,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    goal = await repo.create_goal(task_id, payload)
    return goal.model_dump(mode="json")


@router.patch("/goals/{goal_id}")
async def update_goal(
    goal_id: UUID,
    payload: GoalUpdate,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    goal = await repo.update_goal(goal_id, payload)
    if not goal:
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    return goal.model_dump(mode="json")


@router.delete("/goals/{goal_id}")
async def delete_goal(
    goal_id: UUID,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    repo = _get_repo(request)
    ok = await repo.delete_goal(goal_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    return {"deleted": str(goal_id)}
