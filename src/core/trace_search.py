"""Cross-worker trace search with LRU eviction and TTL-based expiration."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TraceEntry(BaseModel):
    """A single observation recorded by a worker for cross-worker sharing."""
    entry_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    originating_worker_id: str
    intent_id: UUID
    query_terms: List[str] = []
    observation: str
    relevance_score: float = 1.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ttl_seconds: int = 300  # entries expire after this many seconds


class TraceSearchIndex:
    """
    In-memory LRU search index with TTL expiration.
    Max size enforced; oldest entries evicted on overflow.
    Expired entries are cleaned up lazily on each search/add.
    """

    def __init__(self, *, max_size: int = 5000, default_ttl_seconds: int = 300) -> None:
        self._max_size = max_size
        self._default_ttl = timedelta(seconds=default_ttl_seconds)
        # OrderedDict doubles as LRU cache: touch on access, evict from head on overflow
        self._entries: OrderedDict[UUID, TraceEntry] = OrderedDict()

    def add(self, entry: TraceEntry) -> None:
        """Add an entry; evicts oldest if max_size exceeded."""
        self._evict_expired()
        if entry.entry_id in self._entries:
            self._entries.move_to_end(entry.entry_id)
            self._entries[entry.entry_id] = entry
        else:
            self._entries[entry.entry_id] = entry
            if len(self._entries) > self._max_size:
                self._entries.popitem(last=False)  # evict oldest

    def search(
        self,
        query: str,
        task_id: UUID,
        exclude_intent_id: Optional[UUID] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Keyword overlap search with TTL filtering."""
        self._evict_expired()
        query_terms = set(re.findall(r"\w+", query.lower()))
        if not query_terms:
            return []

        now = datetime.utcnow()
        scored: List[tuple[float, TraceEntry]] = []
        for entry in self._entries.values():
            if entry.task_id != task_id:
                continue
            if exclude_intent_id and entry.intent_id == exclude_intent_id:
                continue
            # Check TTL
            if now - entry.timestamp > timedelta(seconds=entry.ttl_seconds):
                continue
            entry_terms = set(entry.query_terms) | set(re.findall(r"\w+", entry.observation.lower()))
            overlap = query_terms & entry_terms
            if not overlap:
                continue
            score = len(overlap) / max(len(query_terms), 1) * entry.relevance_score
            scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, entry in scored[:top_k]:
            results.append({
                "entry_id": str(entry.entry_id),
                "originating_worker_id": entry.originating_worker_id,
                "intent_id": str(entry.intent_id),
                "observation": entry.observation,
                "relevance_score": round(score, 3),
                "timestamp": entry.timestamp.isoformat(),
            })
        return results

    def _evict_expired(self) -> None:
        """Remove entries past their TTL (lazy cleanup)."""
        now = datetime.utcnow()
        expired = [
            eid for eid, e in self._entries.items()
            if now - e.timestamp > timedelta(seconds=e.ttl_seconds)
        ]
        for eid in expired:
            del self._entries[eid]

    def clear(self, task_id: Optional[UUID] = None) -> None:
        if task_id:
            self._entries = OrderedDict(
                (eid, e) for eid, e in self._entries.items() if e.task_id != task_id
            )
        else:
            self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def max_size(self) -> int:
        return self._max_size


# Module-level singleton
_trace_index: Optional[TraceSearchIndex] = None


def get_trace_index() -> TraceSearchIndex:
    global _trace_index
    if _trace_index is None:
        _trace_index = TraceSearchIndex()
    return _trace_index


def reset_trace_index() -> None:
    global _trace_index
    _trace_index = None


def search_worker_traces(
    query: str,
    task_id: UUID,
    exclude_intent_id: Optional[UUID] = None,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Public API: search cross-worker traces for a given query."""
    return get_trace_index().search(query, task_id, exclude_intent_id, top_k)
