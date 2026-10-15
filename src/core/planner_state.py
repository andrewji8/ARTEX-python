"""Planner state schema — consumed by the LangGraph Planner node."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class IntentStep(BaseModel):
    """A single intent on the shared todolist."""
    intent_id: UUID
    title: str
    description: str
    node_type: str = "intent"
    prerequisite_fact_ids: List[UUID] = Field(default_factory=list)
    priority: int = 0
    status: str = "pending"  # pending | claimed | running | completed | failed
    assigned_worker_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    result_node_id: Optional[UUID] = None


class PlannerState(BaseModel):
    """
    Complete state carried through the Planner LangGraph cycle.
    Updated after every Planner run so the Shared Todolist persists.
    """
    task_id: UUID
    current_graph_summary: Dict[str, Any] = Field(default_factory=dict)
    todolist: List[IntentStep] = Field(default_factory=list)
    frontier: List[IntentStep] = Field(default_factory=list)
    last_wake_reason: str = "initial"
    cycle_count: int = 0
    completed_intent_ids: set[UUID] = Field(default_factory=set)
    failed_intent_ids: set[UUID] = Field(default_factory=set)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def pending_intents(self) -> List[IntentStep]:
        return [s for s in self.todolist if s.status == "pending"]

    @property
    def available_facts(self) -> List[UUID]:
        facts = self.current_graph_summary.get("facts", [])
        return [f.get("id") for f in facts if isinstance(f, dict)]

    def has_prerequisites_met(self, step: IntentStep) -> bool:
        prereqs = step.prerequisite_fact_ids
        if not prereqs:
            return True
        return all(fid in self.available_facts for fid in prereqs)
