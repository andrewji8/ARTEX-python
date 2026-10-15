"""Shared pytest fixtures for the ARTEX-python test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable as `src.*`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons between tests for isolation."""
    from src.core import trace_search
    from src.tools import notifier

    trace_search.reset_trace_index()
    notifier.reset_dispatcher()
    yield
    trace_search.reset_trace_index()
    notifier.reset_dispatcher()
