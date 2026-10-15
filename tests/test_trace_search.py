"""Tests for cross-worker trace search (src/core/trace_search.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from src.core.trace_search import (
    TraceEntry,
    TraceSearchIndex,
    get_trace_index,
    reset_trace_index,
    search_worker_traces,
)


def _entry(task_id, worker="w1", intent_id=None, observation="found port 80 open", ttl=300):
    return TraceEntry(
        task_id=task_id,
        originating_worker_id=worker,
        intent_id=intent_id or uuid4(),
        query_terms=["investigation"],
        observation=observation,
        ttl_seconds=ttl,
    )


class TestTraceSearchIndex:
    def test_add_and_search(self):
        idx = TraceSearchIndex()
        tid = uuid4()
        idx.add(_entry(tid, observation="discovered nginx server"))
        results = idx.search("nginx", tid)
        assert len(results) == 1
        assert "nginx" in results[0]["observation"]

    def test_search_filters_by_task(self):
        idx = TraceSearchIndex()
        t1, t2 = uuid4(), uuid4()
        idx.add(_entry(t1, observation="nginx"))
        idx.add(_entry(t2, observation="nginx"))
        assert len(idx.search("nginx", t1)) == 1

    def test_search_excludes_intent(self):
        idx = TraceSearchIndex()
        tid = uuid4()
        iid = uuid4()
        idx.add(_entry(tid, intent_id=iid, observation="nginx"))
        results = idx.search("nginx", tid, exclude_intent_id=iid)
        assert results == []

    def test_no_overlap_returns_empty(self):
        idx = TraceSearchIndex()
        tid = uuid4()
        idx.add(_entry(tid, observation="nginx"))
        assert idx.search("completely different zzz", tid) == []

    def test_lru_eviction_on_overflow(self):
        idx = TraceSearchIndex(max_size=3)
        tid = uuid4()
        for i in range(5):
            idx.add(_entry(tid, observation=f"obs{i} shared"))
        assert idx.size == 3  # oldest evicted

    def test_ttl_expiry(self):
        idx = TraceSearchIndex()
        tid = uuid4()
        # Entry already expired (timestamp in the past).
        old = _entry(tid, observation="nginx expired")
        old.timestamp = datetime.utcnow() - timedelta(seconds=400)
        idx.add(old)
        assert idx.search("nginx", tid) == []

    def test_clear_specific_task(self):
        idx = TraceSearchIndex()
        t1, t2 = uuid4(), uuid4()
        idx.add(_entry(t1, observation="nginx"))
        idx.add(_entry(t2, observation="nginx"))
        idx.clear(t1)
        assert idx.search("nginx", t1) == []
        assert len(idx.search("nginx", t2)) == 1

    def test_clear_all(self):
        idx = TraceSearchIndex()
        tid = uuid4()
        idx.add(_entry(tid))
        idx.clear()
        assert idx.size == 0

    def test_top_k_limit(self):
        idx = TraceSearchIndex()
        tid = uuid4()
        for i in range(10):
            idx.add(_entry(tid, observation=f"shared obs{i}"))
        results = idx.search("shared", tid, top_k=3)
        assert len(results) == 3


class TestSingletons:
    def test_get_and_reset(self):
        reset_trace_index()
        a = get_trace_index()
        b = get_trace_index()
        assert a is b  # singleton
        reset_trace_index()
        c = get_trace_index()
        assert c is not a

    def test_search_worker_traces_helper(self):
        reset_trace_index()
        tid = uuid4()
        get_trace_index().add(_entry(tid, observation="apache server"))
        results = search_worker_traces("apache", tid)
        assert len(results) == 1
