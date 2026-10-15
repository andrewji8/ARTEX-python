"""Dashboard, usage, and graph routes — aggregates for the ARTEX web UI."""

from __future__ import annotations

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.dependencies import get_current_active_user, User
from src.db.repository import AsyncGraphRepository

router = APIRouter(prefix="/api", tags=["dashboard"])


def _get_repo(request: Request) -> AsyncGraphRepository:
    return request.state.repo


@router.get("/stats")
async def get_stats(
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Dashboard overview: task/asset/finding counts + findings by severity."""
    repo = _get_repo(request)
    stats = await repo.get_stats()
    return stats.model_dump(mode="json")


@router.get("/usage")
async def get_usage(
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Token usage: total, daily buckets, by-model, by-tool."""
    repo = _get_repo(request)
    usage = await repo.get_usage()
    return usage.model_dump(mode="json")


@router.get("/tasks/{task_id}/graph")
async def get_task_graph(
    task_id: UUID,
    request: Request,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Exploration graph (nodes + edges) for a task, for graph visualization."""
    repo = _get_repo(request)
    graph = await repo.get_graph(task_id)
    return graph.model_dump(mode="json")


@router.get("/activities/recent")
async def recent_activities(
    request: Request,
    limit: int = 30,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Most recent activities across all tasks, for the dashboard feed."""
    from sqlalchemy import select
    from src.db.models import Activity

    repo = _get_repo(request)
    stmt = select(Activity).order_by(Activity.created_at.desc()).limit(limit)
    result = await repo.db.execute(stmt)
    activities = result.scalars().all()
    return {
        "items": [
            {
                "activity_type": a.activity_type,
                "agent_thought": a.agent_thought,
                "title": a.tool_name,
                "tool_output": a.tool_output,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activities
        ],
        "total": len(activities),
    }


@router.get("/companies/")
async def list_companies(
    request: Request,
    limit: int = 200,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """List companies (for task creation)."""
    repo = _get_repo(request)
    companies, total = await repo.list_companies(limit=limit)
    return {"items": [c.model_dump(mode="json") for c in companies], "total": total}


@router.post("/companies/", status_code=201)
async def create_company(
    request: Request,
    payload: Dict[str, Any],
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Create a company."""
    repo = _get_repo(request)
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    company = await repo.create_company(name=name, **{k: v for k, v in payload.items() if k != "name"})
    return company.model_dump(mode="json")
