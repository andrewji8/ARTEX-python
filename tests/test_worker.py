"""Tests for the Worker (src/core/worker.py) with fake pool + LLM."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from src.core.trace_search import get_trace_index, reset_trace_index
from src.core.worker import (
    _llm_tool_execution,
    _simulate_tool_execution,
    build_worker_graph,
    claim_and_execute,
)
from src.core.worker_state import WorkerState


@dataclass
class FakeLLMResult:
    success: bool
    content: str = ""
    error: str = None
    model: str = "mock-model"
    provider: str = "openai"
    attempts: int = 1
    latency_ms: float = 12.0

    @property
    def token_usage(self):
        class U:
            input_tokens = 10
            output_tokens = 5
            total_tokens = 15
        return U()


class FakeLLMPool:
    def __init__(self, result: FakeLLMResult):
        self._result = result
        self.calls = []

    async def complete(self, prompt, *, system=None):
        self.calls.append((prompt, system))
        return self._result


class FakePool:
    def __init__(self):
        self.completed = []

    async def mark_intent_completed(self, task_id, intent_id, worker_id):
        self.completed.append((task_id, intent_id, worker_id))


def _worker_state(intent=None, task_id=None, worker_id="worker-0"):
    return WorkerState(
        task_id=task_id or uuid4(),
        worker_id=worker_id,
        intent=intent,
    )


def _intent(title="Scan target", description="run nmap"):
    iid = uuid4()
    return {
        "intent_id": str(iid),
        "title": title,
        "description": description,
        "node_type": "intent",
        "priority": 50,
        "task_id": str(uuid4()),
        "status": "claimed",
    }, iid


class TestSimulateToolExecution:
    @pytest.mark.asyncio
    async def test_returns_activity(self):
        act = await _simulate_tool_execution("Scan", "w1")
        assert act.activity_type == "tool_result"
        assert act.tool_name == "mock_recon_tool"
        assert "port_scan" in act.tool_output


class TestClaimAndExecute:
    @pytest.mark.asyncio
    async def test_idle_when_no_intent(self):
        reset_trace_index()
        pool = FakePool()
        state = _worker_state(intent=None)
        result = await claim_and_execute(state, repo=None, pool=pool)
        assert result["status"] == "idle"
        assert result["activities"] == []

    @pytest.mark.asyncio
    async def test_simulation_marks_completed(self):
        reset_trace_index()
        pool = FakePool()
        intent, iid = _intent()
        state = _worker_state(intent=intent)
        result = await claim_and_execute(state, repo=None, pool=pool)
        assert result["status"] == "done"
        assert len(pool.completed) == 1
        assert pool.completed[0][1] == iid
        assert result["result_node_id"] is not None

    @pytest.mark.asyncio
    async def test_records_trace_for_cross_worker(self):
        reset_trace_index()
        pool = FakePool()
        intent, iid = _intent(description="look for apache server")
        state = _worker_state(intent=intent)
        await claim_and_execute(state, repo=None, pool=pool)
        # A trace was recorded and is searchable.
        assert get_trace_index().size >= 1


class TestLLMToolExecution:
    @pytest.mark.asyncio
    async def test_uses_llm_content(self):
        pool = FakePool()
        llm = FakeLLMPool(FakeLLMResult(success=True, content="LLM analysis result"))
        intent, iid = _intent(title="Investigate")
        state = _worker_state(intent=intent)
        result = await claim_and_execute(state, repo=None, pool=pool, llm_pool=llm)
        assert result["status"] == "done"
        act = result["activities"][0]
        assert act.tool_name == "llm:mock-model"
        assert act.agent_thought == "LLM analysis result"
        assert act.tool_output["analysis"] == "LLM analysis result"
        assert act.extra["token_usage"]["total"] == 15

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_simulation(self):
        pool = FakePool()
        llm = FakeLLMPool(FakeLLMResult(success=False, error="provider down"))
        intent, iid = _intent(title="Investigate")
        state = _worker_state(intent=intent)
        result = await claim_and_execute(state, repo=None, pool=pool, llm_pool=llm)
        assert result["status"] == "done"
        act = result["activities"][0]
        assert act.tool_name == "mock_recon_tool"  # fell back

    @pytest.mark.asyncio
    async def test_system_prompt_passed(self):
        llm = FakeLLMPool(FakeLLMResult(success=True, content="ok"))
        await _llm_tool_execution("title", "w1", llm_pool=llm)
        assert llm.calls[0][1] is not None  # system prompt present


class TestBuildWorkerGraph:
    def test_graph_compiles(self):
        pool = FakePool()
        graph = build_worker_graph(None, pool)
        assert graph is not None

    def test_graph_compiles_with_llm(self):
        pool = FakePool()
        llm = FakeLLMPool(FakeLLMResult(success=True, content="ok"))
        graph = build_worker_graph(None, pool, llm_pool=llm)
        assert graph is not None
