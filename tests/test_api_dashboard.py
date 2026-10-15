"""API-level tests for findings/goals/dashboard routes with a stubbed DB session.

The middleware injects ``request.state.repo`` from ``session_factory``. We swap
``session_factory`` for a fake async context manager yielding a fake session,
and stub the repository methods the routes call, so we exercise the full
route -> serialize path without a real database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import src.main as main_module
from src.db.schemas import FindingOut, GoalOut, Stats, UsageStats, GraphData


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FakeSession:
    async def commit(self): pass
    async def rollback(self): pass


def _install_fake_repo(monkeypatch, *, stats=None, usage=None, graph=None, findings=None, goals=None):
    """Patch session_factory so middleware builds a repo with stubbed methods."""

    class StubRepo:
        def __init__(self, db):
            self.db = db

        async def get_stats(self):
            return stats or Stats()

        async def get_usage(self):
            return usage or UsageStats()

        async def get_graph(self, task_id):
            return graph or GraphData(task_id=str(task_id))

        async def findings_by_severity(self, task_id=None):
            return {"critical": 1, "high": 2, "medium": 0, "low": 0}

        async def list_findings(self, task_id, **kw):
            return (findings or []), len(findings or [])

        async def create_finding(self, task_id, data):
            now = _now()
            return FindingOut(
                id=uuid4(), task_id=task_id, title=data.title,
                severity=data.severity.value, status=data.status.value,
                confidence=0, created_at=now, updated_at=now,
            )

        async def list_goals(self, task_id):
            return goals or []

        async def goal_counts(self, task_id):
            return (len(goals or []), sum(1 for g in (goals or []) if g.status == "met"))

        async def create_goal(self, task_id, data):
            now = _now()
            return GoalOut(id=uuid4(), task_id=task_id, title=data.title, status=data.status.value,
                           priority=data.priority or 0, created_at=now, updated_at=now)

        async def list_companies(self, **kw):
            return [], 0

        async def get_task(self, task_id):
            return None

    @asynccontextmanager
    async def fake_factory():
        yield FakeSession()

    # Patch the factory used by middleware AND the repo constructor.
    import src.db.repository as repo_mod
    real_repo_cls = repo_mod.AsyncGraphRepository
    monkeypatch.setattr(main_module, "session_factory", fake_factory)
    monkeypatch.setattr(main_module, "AsyncGraphRepository", StubRepo)


@pytest.fixture
def client(monkeypatch):
    _install_fake_repo(monkeypatch)
    with TestClient(main_module.app) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c


class TestDashboardRoutes:
    def test_stats(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert "tasks_total" in body
        assert body["findings"]["critical"] == 0

    def test_usage(self, client):
        r = client.get("/api/usage")
        assert r.status_code == 200
        assert "total" in r.json()

    def test_graph(self, client):
        tid = uuid4()
        r = client.get(f"/api/tasks/{tid}/graph")
        assert r.status_code == 200
        assert r.json()["task_id"] == str(tid)
        assert r.json()["nodes"] == []

    def test_stats_requires_auth(self, monkeypatch):
        _install_fake_repo(monkeypatch)
        with TestClient(main_module.app) as c:
            assert c.get("/api/stats").status_code == 401


class TestFindingsRoutes:
    def test_list_findings(self, client):
        tid = uuid4()
        r = client.get(f"/api/tasks/{tid}/findings")
        assert r.status_code == 200
        assert r.json()["by_severity"]["critical"] == 1
        assert r.json()["items"] == []

    def test_create_finding(self, client):
        tid = uuid4()
        r = client.post(f"/api/tasks/{tid}/findings", json={"title": "SQLi", "severity": "critical"})
        assert r.status_code == 201
        body = r.json()
        assert body["title"] == "SQLi"
        assert body["severity"] == "critical"

    def test_create_finding_validation(self, client):
        tid = uuid4()
        r = client.post(f"/api/tasks/{tid}/findings", json={"severity": "high"})
        assert r.status_code == 422  # title required


class TestGoalsRoutes:
    def test_list_goals(self, client):
        tid = uuid4()
        r = client.get(f"/api/tasks/{tid}/goals")
        assert r.status_code == 200
        assert r.json()["goals_total"] == 0

    def test_create_goal(self, client):
        tid = uuid4()
        r = client.post(f"/api/tasks/{tid}/goals", json={"title": "拿到后台权限"})
        assert r.status_code == 201
        assert r.json()["title"] == "拿到后台权限"
