"""Tests for Pydantic schemas and DB models (src/db/schemas.py, models.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.db.schemas import (
    ActivityCreate,
    ActivityTypeEnum,
    AnchorTypeEnum,
    AssetCreate,
    AssetTypeEnum,
    CompanyCreate,
    ExplorationNodeCreate,
    ExplorationNodeTypeEnum,
    TaskCreate,
    TaskStatusEnum,
    TaskTypeEnum,
)


class TestCompanyCreate:
    def test_valid(self):
        c = CompanyCreate(name="Acme", domain="acme.com", contact_email="a@acme.com")
        assert c.name == "Acme"

    def test_name_required(self):
        with pytest.raises(ValidationError):
            CompanyCreate()

    def test_name_max_length(self):
        with pytest.raises(ValidationError):
            CompanyCreate(name="x" * 256)


class TestAssetCreate:
    def test_valid_domain_asset(self):
        a = AssetCreate(asset_type=AssetTypeEnum.domain, domain="example.com")
        assert a.asset_type == AssetTypeEnum.domain

    def test_port_range(self):
        with pytest.raises(ValidationError):
            AssetCreate(asset_type=AssetTypeEnum.service, port=99999)

    def test_valid_port(self):
        a = AssetCreate(asset_type=AssetTypeEnum.service, port=443)
        assert a.port == 443


class TestTaskCreate:
    def test_valid(self):
        from uuid import UUID
        cid = UUID("11111111-1111-1111-1111-111111111111")
        t = TaskCreate(company_id=cid, task_type=TaskTypeEnum.recon, title="Scan prod")
        assert t.company_id == cid
        assert t.task_type == TaskTypeEnum.recon
        assert t.title == "Scan prod"

    def test_title_required(self):
        from uuid import UUID
        cid = UUID("11111111-1111-1111-1111-111111111111")
        with pytest.raises(ValidationError):
            TaskCreate(company_id=cid, task_type=TaskTypeEnum.recon)


    def test_company_id_required(self):
        with pytest.raises(ValidationError):
            TaskCreate(task_type=TaskTypeEnum.recon, title="x")

class TestExplorationNodeCreate:
    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            ExplorationNodeCreate(node_type=ExplorationNodeTypeEnum.fact, title="t", confidence=200)

    def test_valid_confidence(self):
        n = ExplorationNodeCreate(node_type=ExplorationNodeTypeEnum.fact, title="t", confidence=50)
        assert n.confidence == 50


class TestActivityCreate:
    def test_valid(self):
        a = ActivityCreate(activity_type=ActivityTypeEnum.tool_call, tool_name="nmap")
        assert a.tool_name == "nmap"

    def test_duration_non_negative(self):
        with pytest.raises(ValidationError):
            ActivityCreate(activity_type=ActivityTypeEnum.tool_call, duration_ms=-1)


class TestEnums:
    def test_enum_values(self):
        assert TaskTypeEnum.recon.value == "recon"
        assert AnchorTypeEnum.supports.value == "supports"
        assert TaskStatusEnum.completed.value == "completed"
        assert ActivityTypeEnum.error.value == "error"
