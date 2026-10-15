"""Planner LangGraph — decides what to do next based on graph state."""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional
from uuid import UUID

from langgraph.graph import StateGraph, END

from src.core.planner_state import IntentStep, PlannerState
from src.db.repository import AsyncGraphRepository

logger = logging.getLogger(__name__)


def _infer_new_intents(
    task_id: UUID,
    graph_summary: Dict[str, Any],
    todolist: List[IntentStep],
    repository: AsyncGraphRepository,
) -> List[IntentStep]:
    """
    Heuristic planner: scan exploration_nodes for unmet-prerequisite steps
    and generate new intents for the frontier.
    """
    facts = graph_summary.get("facts", [])
    findings = graph_summary.get("findings", [])
    clues = graph_summary.get("clues", [])

    known_fact_ids = {str(f["id"]) for f in facts if isinstance(f, dict)}
    existing_intent_ids = {str(s.intent_id) for s in todolist}

    new_intents: List[IntentStep] = []

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        fid = finding.get("id")
        if fid in existing_intent_ids:
            continue
        prereqs = [f for f in known_fact_ids if f != fid]
        new_intents.append(IntentStep(
            intent_id=fid,
            title=finding.get("title", "Follow-up investigation"),
            description=finding.get("content", ""),
            prerequisite_fact_ids=prereqs[:3],
            priority=finding.get("confidence", 50),
        ))

    for clue in clues:
        if not isinstance(clue, dict):
            continue
        cid = clue.get("id")
        if cid in existing_intent_ids:
            continue
        new_intents.append(IntentStep(
            intent_id=cid,
            title=clue.get("title", "Investigate clue"),
            description=clue.get("content", ""),
            prerequisite_fact_ids=[],
            priority=clue.get("confidence", 30),
        ))

    new_intents.sort(key=lambda s: s.priority, reverse=True)
    return new_intents


async def plan_next_intents(state: PlannerState, *, repo: AsyncGraphRepository) -> Dict[str, Any]:
    """
    LangGraph node: reads current graph state, appends newly inferable
    intents to the frontier, and updates the Shared Todolist.
    """
    context = await repo.get_task_context(state.task_id)
    if context is None:
        logger.warning("planner: task %s not found", state.task_id)
        return {"frontier": state.frontier, "cycle_count": state.cycle_count}

    graph_summary: Dict[str, Any] = {
        "facts": [
            {"id": str(n.id), "title": n.title, "content": n.content, "confidence": n.confidence}
            for n in context.nodes
            if n.node_type == "fact"
        ],
        "findings": [
            {"id": str(n.id), "title": n.title, "content": n.content, "confidence": n.confidence}
            for n in context.nodes
            if n.node_type == "finding"
        ],
        "clues": [
            {"id": str(n.id), "title": n.title, "content": n.content, "confidence": n.confidence}
            for n in context.nodes
            if n.node_type == "clue"
        ],
        "goals": [
            {"id": str(n.id), "title": n.title}
            for n in context.nodes
            if n.node_type == "goal"
        ],
        "total_activities": context.total_activities,
    }

    todolist = list(state.todolist)
    new_intents = _infer_new_intents(state.task_id, graph_summary, todolist, repo)

    for intent in new_intents:
        if intent.intent_id not in {s.intent_id for s in todolist}:
            todolist.append(intent)

    # Move ready (prerequisites met) intents to frontier
    frontier: List[IntentStep] = []
    known_fact_str_ids = {str(f["id"]) for f in graph_summary["facts"]}
    for step in todolist:
        if step.status == "pending" and step.intent_id not in {s.intent_id for s in frontier}:
            if step.prerequisite_fact_ids and not all(
                str(fid) in known_fact_str_ids
                for fid in step.prerequisite_fact_ids
            ):
                continue
            frontier.append(step)

    frontier.sort(key=lambda s: s.priority, reverse=True)

    return {
        "current_graph_summary": graph_summary,
        "todolist": todolist,
        "frontier": frontier,
        "last_wake_reason": "graph_change",
        "cycle_count": state.cycle_count + 1,
    }


def build_planner_graph(repo: AsyncGraphRepository) -> StateGraph:
    """Build and compile the Planner LangGraph."""

    async def _plan_node(s: PlannerState) -> Dict[str, Any]:
        return await plan_next_intents(s, repo=repo)

    graph = StateGraph(PlannerState)
    graph.add_node("plan", _plan_node)
    graph.set_entry_point("plan")
    graph.add_edge("plan", END)
    return graph.compile()
