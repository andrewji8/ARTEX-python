"""Tests for the new ARTEX-aligned schemas, models, and route wiring."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.db.schemas import (
    FindingCreate,
    FindingUpdate,
    GoalCreate,
    GoalUpdate,
    SeverityEnum,
    FindingStatusEnum,
    GoalStatusEnum,
    Stats,
    FindingsBySeverity,
    TokenTotal,
    UsageStats,
    GraphData,
    GraphNode,
    GraphEdge,
)
from src.db.models import Finding, Goal, Severity, FindingStatus, GoalStatus


class TestFindingSchemas:
    def test_valid_finding(self):
        f = FindingCreate(title="SQLi in login", severity=SeverityEnum.critical)
        assert f.severity == SeverityEnum.critical
        assert f.status == FindingStatusEnum.candidate

    def test_title_required(self):
        with pytest.raises(ValidationError):
            FindingCreate()

    def test_title_max_length(self):
        with pytest.raises(ValidationError):
            FindingCreate(title="x" * 501)

    def test_cvss_bounds(self):
        with pytest.raises(ValidationError):
            FindingCreate(title="t", cvss_score=200)
        assert FindingCreate(title="t", cvss_score=85).cvss_score == 85

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            FindingCreate(title="t", confidence=200)


class TestGoalSchemas:
    def test_valid_goal(self):
        g = GoalCreate(title="拿到后台权限")
        assert g.status == GoalStatusEnum.pending

    def test_title_required(self):
        with pytest.raises(ValidationError):
            GoalCreate()

    def test_update_partial(self):
        u = GoalUpdate(status=GoalStatusEnum.met)
        assert u.status == GoalStatusEnum.met
        assert u.title is None


class TestAggregateSchemas:
    def test_token_total_property(self):
        t = TokenTotal(input_tokens=10, output_tokens=5, cache_read_tokens=2, cache_write_tokens=1)
        assert t.total_tokens == 18

    def test_findings_by_severity_defaults(self):
        s = FindingsBySeverity()
        assert s.critical == 0 and s.high == 0

    def test_stats_defaults(self):
        s = Stats()
        assert s.tasks_total == 0
        assert s.findings.critical == 0

    def test_graph_data(self):
        g = GraphData(
            task_id="t1",
            nodes=[GraphNode(id="n1", node_type="goal", title="G")],
            edges=[GraphEdge(id="e1", source="n1", target="n2", anchor_type="leads_to")],
        )
        assert len(g.nodes) == 1
        assert g.edges[0].anchor_type == "leads_to"


class TestModels:
    def test_severity_enum_values(self):
        assert Severity.critical.value == "critical"
        assert Severity.info.value == "info"

    def test_finding_status_values(self):
        assert FindingStatus.confirmed.value == "confirmed"
        assert FindingStatus.false_positive.value == "false_positive"

    def test_goal_status_values(self):
        assert GoalStatus.met.value == "met"

    def test_finding_tablename(self):
        assert Finding.__tablename__ == "findings"

    def test_goal_tablename(self):
        assert Goal.__tablename__ == "goals"


class TestRouteWiring:
    def test_all_new_routes_registered(self):
        from src.main import app
        paths = {r.path for r in app.routes}
        expected = [
            "/api/stats",
            "/api/usage",
            "/api/activities/recent",
            "/api/companies/",
            "/api/tasks/stream",
            "/api/tasks/{task_id}/graph",
            "/api/tasks/{task_id}/findings",
            "/api/tasks/{task_id}/goals",
            "/api/findings/{finding_id}",
            "/api/goals/{goal_id}",
        ]
        for p in expected:
            assert p in paths, f"missing route {p}"
