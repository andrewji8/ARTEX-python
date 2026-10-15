"""Worker state schema — consumed by the LangGraph Worker node."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class WorkerActivity(BaseModel):
    """An activity record emitted by a worker after executing an intent."""
    activity_type: str
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[Dict[str, Any]] = None
    agent_thought: Optional[str] = None
    result_data: Optional[Dict[str, Any]] = None
    duration_ms: Optional[int] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class WorkerState(BaseModel):
    """State carried through a single Worker LangGraph execution cycle."""
    intent: Optional[Dict[str, Any]] = None
    task_id: UUID
    worker_id: str
    activities: List[WorkerActivity] = Field(default_factory=list)
    result_node_id: Optional[UUID] = None
    error: Optional[str] = None
    status: str = "idle"  # idle | working | done | error
    executed_at: Optional[datetime] = None
