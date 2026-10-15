"""Worker LangGraph — claims intents and executes them via a pluggable tool executor."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from langgraph.graph import StateGraph, END

from src.core.trace_search import search_worker_traces, TraceEntry, get_trace_index
from src.tools.real_tools import ToolExecutor
from src.core.worker_state import WorkerActivity, WorkerState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = (
    "You are an autonomous penetration-testing agent. Given an investigation "
    "intent, produce a concise plan and the concrete observations you would "
    "expect. Respond in plain text."
)


async def _simulate_tool_execution(intent_title: str, worker_id: str) -> WorkerActivity:
    """Mock tool execution that returns structured output for graph mutation."""
    await asyncio.sleep(0.05)  # simulate network/tool latency
    return WorkerActivity(
        activity_type="tool_result",
        tool_name="mock_recon_tool",
        tool_input={"intent": intent_title},
        tool_output={
            "discovered_asset": f"10.0.{hash(intent_title) % 256}.{hash(worker_id) % 256}",
            "port_scan": [80, 443, 8080],
            "service_version": "nginx/1.24",
        },
        agent_thought=f"Worker {worker_id} executed tool for intent: {intent_title}",
        duration_ms=42,
    )


async def _llm_tool_execution(
    intent_title: str,
    worker_id: str,
    *,
    llm_pool: Any,
    prior_context: str = "",
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
) -> WorkerActivity:
    """
    Execute an intent using an LLM pool to drive the decision. Falls back to
    the mock executor if the LLM call fails, so the worker never hard-crashes.
    """
    prompt = f"Investigation intent: {intent_title}"
    if prior_context:
        prompt += f"\n\nPrior observations from other workers:\n{prior_context}"

    result = await llm_pool.complete(prompt, system=system_prompt)
    if not result.success:
        logger.warning(
            "worker=%s LLM call failed (%s) — falling back to simulation",
            worker_id, result.error,
        )
        return await _simulate_tool_execution(intent_title, worker_id)

    return WorkerActivity(
        activity_type="tool_result",
        tool_name=f"llm:{result.model or 'unknown'}",
        tool_input={"intent": intent_title, "prior_context": prior_context or None},
        tool_output={
            "analysis": result.content,
            "provider": result.provider,
            "attempts": result.attempts,
        },
        agent_thought=result.content,
        duration_ms=int(result.latency_ms),
        extra={"token_usage": {
            "input": result.token_usage.input_tokens,
            "output": result.token_usage.output_tokens,
            "total": result.token_usage.total_tokens,
        }},
    )


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Tool decision parsing (Route B)
# ---------------------------------------------------------------------------

def _parse_tool_decision(content: str, intent: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort parse of an LLM/agent response into a concrete ToolPlan dict.

    Accepts either a JSON object like ``{"tool": "nmap", "target": "...",
    "args": "-sV -p- "}`` or a bare text answer (in which case we fall back to
    a deterministic ``http_probe`` against the task's default target).
    """
    import json as _json
    import re as _re

    text = (content or "").strip()
    if not text:
        text = "no observation"

    # try to find a JSON object anywhere in the text
    match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if match:
        try:
            obj = _json.loads(match.group(0))
            if isinstance(obj, dict) and obj.get("tool"):
                return {
                    "tool": str(obj.get("tool")),
                    "target": str(obj.get("target", intent.get("title", ""))),
                    "args": str(obj.get("args", "")),
                    "source": "llm",
                }
        except Exception:
            pass

    # deterministic fallback: probe the default health endpoint
    return {
        "tool": "http_probe",
        "target": "http://127.0.0.1:8000/api/health",
        "args": "",
        "source": "rule",
    }



async def claim_and_execute(
    state: WorkerState,
    *,
    repo: Any,
    pool: "WorkerPool",
    llm_pool: Optional[Any] = None,
    tool_executor: Optional[ToolExecutor] = None,
) -> Dict[str, Any]:
    """
    LangGraph node: claims the highest-priority pending intent from the
    Shared Todolist, executes it (via LLM if a pool is supplied, otherwise a
    deterministic simulation), then records the result.
    """
    intent = state.intent
    if intent is None:
        return {"status": "idle", "activities": []}

    intent_id = UUID(intent["intent_id"])
    worker_id = state.worker_id
    task_id = state.task_id

    # Search cross-worker traces before executing
    prior_obs = search_worker_traces(
        query=intent["description"][:80] if intent.get("description") else "investigation",
        task_id=task_id,
        exclude_intent_id=intent_id,
        top_k=3,
    )
    extra_context = " ".join(o["observation"] for o in prior_obs) if prior_obs else ""
    logger.info("worker=%s claiming intent=%s (prior traces=%d)", worker_id, intent_id, len(prior_obs))

    # Execute the tool
    if llm_pool is not None:
        activity = await _llm_tool_execution(
            intent_title=intent.get("title", ""),
            worker_id=worker_id,
            llm_pool=llm_pool,
            prior_context=extra_context,
        )
    else:
        activity = await _simulate_tool_execution(
            intent_title=intent.get("title", ""),
            worker_id=worker_id,
        )

    # Route B: execute a real tool when an executor is wired in.
    if tool_executor is not None:
        decision = _parse_tool_decision(activity.agent_thought or "", intent)
        tool_result = await tool_executor.run(
            tool=decision["tool"],
            target=decision["target"],
            args=decision["args"],
        )
        base_output = dict(activity.tool_output or {})
        base_output["tool_decision"] = decision
        base_output["real_tool"] = tool_result
        activity.tool_output = base_output
        if tool_result.get("status") == "unavailable":
            activity.agent_thought = (
                f"[real tool {decision['tool']} unavailable: {tool_result.get('error')}]"
            )

    # Record observation for cross-worker sharing
    observation = activity.agent_thought or (str(activity.tool_output) if activity.tool_output else "")
    trace_entry = TraceEntry(
        task_id=task_id,
        originating_worker_id=worker_id,
        intent_id=intent_id,
        query_terms=["investigation", intent.get("node_type", "")],
        observation=observation,
        relevance_score=0.8,
    )
    get_trace_index().add(trace_entry)

    # Signal completion back to the pool so the todolist can be updated.
    # (WorkerPool._worker_loop performs the authoritative DB completion via
    # repo.complete_intent; the Engine binding provides this hook as a no-op.)
    if pool is not None and hasattr(pool, "mark_intent_completed"):
        await pool.mark_intent_completed(task_id, intent_id, worker_id)

    return {
        "activities": [activity],
        "result_node_id": UUID(int=hash(intent_id.int) & 0xFFFFFFFFFFFFFFFF),
        "status": "done",
        "executed_at": None,
    }


def build_worker_graph(
    repo: Any,
    pool: "WorkerPool",
    llm_pool: Optional[Any] = None,
    tool_executor: Optional[ToolExecutor] = None,
) -> StateGraph:
    """Build and compile a single Worker LangGraph."""

    async def _execute_node(s: WorkerState) -> Dict[str, Any]:
        return await claim_and_execute(
            s, repo=repo, pool=pool, llm_pool=llm_pool, tool_executor=tool_executor
        )

    graph = StateGraph(WorkerState)
    graph.add_node("execute", _execute_node)
    graph.set_entry_point("execute")
    graph.add_edge("execute", END)
    return graph.compile()
