from datetime import datetime
from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


T = TypeVar("T")


class BaseModelWithID(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class AssetTypeEnum(str, Enum):
    domain = "domain"
    ip = "ip"
    service = "service"
    endpoint = "endpoint"


class RiskLevelEnum(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class TaskTypeEnum(str, Enum):
    recon = "recon"
    scan = "scan"
    exploit = "exploit"
    post_exploit = "post_exploit"
    full = "full"


class TaskStatusEnum(str, Enum):
    pending = "pending"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ExplorationNodeTypeEnum(str, Enum):
    goal = "goal"
    intent = "intent"
    fact = "fact"
    finding = "finding"
    clue = "clue"
    tool_output = "tool_output"
    hypothesis = "hypothesis"
    action = "action"


class AnchorTypeEnum(str, Enum):
    supports = "supports"
    contradicts = "contradicts"
    leads_to = "leads_to"
    related_to = "related_to"
    question_for = "question_for"


class ActivityTypeEnum(str, Enum):
    tool_call = "tool_call"
    tool_result = "tool_result"
    agent_thought = "agent_thought"
    state_update = "state_update"
    milestone = "milestone"
    error = "error"
    human_intervention = "human_intervention"


# ==================== Company Schemas ====================

class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    industry: Optional[str] = Field(None, max_length=100)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_phone: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = Field(default_factory=dict)


class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    industry: Optional[str] = Field(None, max_length=100)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_phone: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    is_active: Optional[int] = None


class CompanyOut(BaseModelWithID):
    name: str
    domain: Optional[str] = None
    industry: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    is_active: int
    created_at: datetime
    updated_at: datetime


# ==================== Asset Schemas ====================

class AssetCreate(BaseModel):
    asset_type: AssetTypeEnum
    ip_address: Optional[str] = Field(None, max_length=45)
    domain: Optional[str] = Field(None, max_length=255)
    port: Optional[int] = Field(None, ge=0, le=65535)
    service_name: Optional[str] = Field(None, max_length=100)
    protocol: Optional[str] = Field(None, max_length=20)
    os_guess: Optional[str] = Field(None, max_length=100)
    risk_level: Optional[RiskLevelEnum] = None
    tags: Optional[list[str]] = Field(default_factory=list)
    extra_data: Optional[dict[str, Any]] = Field(default_factory=dict)
    last_seen_at: Optional[datetime] = None

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.strip():
            raise ValueError("domain cannot be empty string")
        return v.strip() if v else v

    @field_validator("ip_address")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.strip():
            raise ValueError("ip_address cannot be empty string")
        return v.strip() if v else v

    def model_post_init(self, __context: Any) -> None:
        if self.asset_type == AssetTypeEnum.domain and self.domain:
            if not self.ip_address:
                self.ip_address = None
        elif self.asset_type == AssetTypeEnum.ip and self.ip_address:
            pass


class AssetUpsertPayload(BaseModel):
    asset_type: AssetTypeEnum
    ip_address: Optional[str] = Field(None, max_length=45)
    domain: Optional[str] = Field(None, max_length=255)
    port: Optional[int] = Field(None, ge=0, le=65535)
    service_name: Optional[str] = Field(None, max_length=100)
    protocol: Optional[str] = Field(None, max_length=20)
    os_guess: Optional[str] = Field(None, max_length=100)
    risk_level: Optional[RiskLevelEnum] = None
    tags: Optional[list[str]] = Field(default_factory=list)
    extra_data: Optional[dict[str, Any]] = Field(default_factory=dict)
    last_seen_at: Optional[datetime] = None


class AssetOut(BaseModelWithID):
    company_id: UUID
    asset_type: str
    ip_address: Optional[str] = None
    domain: Optional[str] = None
    port: Optional[int] = None
    service_name: Optional[str] = None
    protocol: Optional[str] = None
    os_guess: Optional[str] = None
    risk_level: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    extra_data: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: Optional[datetime] = None
    is_active: int
    created_at: datetime
    updated_at: datetime


# ==================== Task Schemas ====================

class TaskCreate(BaseModel):
    company_id: UUID
    task_type: TaskTypeEnum
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    priority: Optional[int] = Field(0, ge=-100, le=100)
    max_iterations: Optional[int] = Field(None, ge=1)
    agent_config: Optional[dict[str, Any]] = Field(default_factory=dict)
    context_data: Optional[dict[str, Any]] = Field(default_factory=dict)
    asset_id: Optional[UUID] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[TaskStatusEnum] = None
    priority: Optional[int] = Field(None, ge=-100, le=100)
    agent_config: Optional[dict[str, Any]] = None
    context_data: Optional[dict[str, Any]] = None
    result_summary: Optional[str] = None


class TaskOut(BaseModelWithID):
    company_id: UUID
    asset_id: Optional[UUID] = None
    task_type: str
    title: str
    description: Optional[str] = None
    status: str
    priority: int
    max_iterations: Optional[int] = None
    current_iteration: int
    agent_config: dict[str, Any] = Field(default_factory=dict)
    context_data: dict[str, Any] = Field(default_factory=dict)
    result_summary: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ==================== Exploration Node Schemas ====================

class ExplorationNodeCreate(BaseModel):
    node_type: ExplorationNodeTypeEnum
    title: str = Field(..., min_length=1, max_length=255)
    content: Optional[str] = None
    confidence: Optional[int] = Field(0, ge=0, le=100)
    source: Optional[str] = Field(None, max_length=100)
    tool_name: Optional[str] = Field(None, max_length=100)
    extra_data: Optional[dict[str, Any]] = Field(default_factory=dict)
    is_verified: Optional[int] = Field(0, ge=0, le=1)
    depth_level: Optional[int] = Field(0, ge=0)


class ExplorationNodeOut(BaseModelWithID):
    task_id: UUID
    node_type: str
    title: str
    content: Optional[str] = None
    confidence: int
    source: Optional[str] = None
    tool_name: Optional[str] = None
    extra_data: dict[str, Any] = Field(default_factory=dict)
    is_verified: int
    depth_level: int
    anchors: List["AnchorSummary"] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AnchorSummary(BaseModel):
    id: UUID
    anchor_type: str
    from_node_id: UUID
    to_node_id: UUID
    weight: int
    notes: Optional[str] = None
    created_at: datetime


# ==================== Anchor Schemas ====================

class AnchorCreate(BaseModel):
    from_node_id: UUID
    to_node_id: UUID
    anchor_type: AnchorTypeEnum
    weight: Optional[int] = Field(1, ge=0, le=10)
    notes: Optional[str] = None


class AnchorOut(BaseModelWithID):
    from_node_id: UUID
    to_node_id: UUID
    anchor_type: str
    weight: int
    notes: Optional[str] = None
    from_node: Optional[ExplorationNodeOut] = None
    to_node: Optional[ExplorationNodeOut] = None
    created_at: datetime


# ==================== Activity Schemas ====================

class ActivityCreate(BaseModel):
    activity_type: ActivityTypeEnum
    intent_id: Optional[UUID] = None
    step_number: Optional[int] = Field(0, ge=0)
    sequence_id: Optional[str] = Field(None, max_length=50)
    tool_name: Optional[str] = Field(None, max_length=100)
    tool_input: Optional[dict[str, Any]] = None
    tool_output: Optional[dict[str, Any]] = None
    agent_thought: Optional[str] = None
    result_data: Optional[dict[str, Any]] = None
    metadata_json: Optional[dict[str, Any]] = Field(default_factory=dict)
    duration_ms: Optional[int] = Field(None, ge=0)
    error_message: Optional[str] = None


class ActivityOut(BaseModelWithID):
    task_id: UUID
    intent_id: Optional[UUID] = None
    activity_type: str
    step_number: int
    sequence_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None
    tool_output: Optional[dict[str, Any]] = None
    agent_thought: Optional[str] = None
    result_data: Optional[dict[str, Any]] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ==================== Task Context (aggregated) ====================

class TaskContextData(BaseModel):
    task: TaskOut
    nodes: List[ExplorationNodeOut] = Field(default_factory=list)
    anchors: List[AnchorOut] = Field(default_factory=list)
    activities: List[ActivityOut] = Field(default_factory=list)
    total_nodes: int = 0
    total_anchors: int = 0
    total_activities: int = 0


# ==================== Findings ====================

class SeverityEnum(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class FindingStatusEnum(str, Enum):
    candidate = "candidate"
    confirmed = "confirmed"
    false_positive = "false_positive"
    fixed = "fixed"
    retesting = "retesting"


class FindingCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    severity: SeverityEnum = SeverityEnum.medium
    status: FindingStatusEnum = FindingStatusEnum.candidate
    vuln_type: Optional[str] = Field(None, max_length=100)
    affected_asset: Optional[str] = Field(None, max_length=500)
    cvss_score: Optional[int] = Field(None, ge=0, le=100)
    evidence: Optional[str] = None
    remediation: Optional[str] = None
    source_node_id: Optional[UUID] = None
    confidence: Optional[int] = Field(0, ge=0, le=100)
    extra_data: Optional[dict[str, Any]] = Field(default_factory=dict)


class FindingUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    severity: Optional[SeverityEnum] = None
    status: Optional[FindingStatusEnum] = None
    vuln_type: Optional[str] = None
    affected_asset: Optional[str] = None
    cvss_score: Optional[int] = Field(None, ge=0, le=100)
    evidence: Optional[str] = None
    remediation: Optional[str] = None
    confidence: Optional[int] = Field(None, ge=0, le=100)


class FindingOut(BaseModelWithID):
    task_id: UUID
    title: str
    description: Optional[str] = None
    severity: str
    status: str
    vuln_type: Optional[str] = None
    affected_asset: Optional[str] = None
    cvss_score: Optional[int] = None
    evidence: Optional[str] = None
    remediation: Optional[str] = None
    source_node_id: Optional[UUID] = None
    confidence: int
    extra_data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ==================== Goals ====================

class GoalStatusEnum(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    met = "met"
    failed = "failed"


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status: GoalStatusEnum = GoalStatusEnum.pending
    priority: Optional[int] = Field(0, ge=-100, le=100)
    extra_data: Optional[dict[str, Any]] = Field(default_factory=dict)


class GoalUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status: Optional[GoalStatusEnum] = None
    priority: Optional[int] = Field(None, ge=-100, le=100)


class GoalOut(BaseModelWithID):
    task_id: UUID
    title: str
    description: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status: str
    priority: int
    met_at: Optional[datetime] = None
    extra_data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ==================== Dashboard / Stats (对齐 ARTEX Stats) ====================

class FindingsBySeverity(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0


class TokenTotal(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


class DailyTokenBucket(BaseModel):
    date: str  # YYYY-MM-DD
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ModelTokenStat(BaseModel):
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ToolStat(BaseModel):
    tool: str
    calls: int = 0


class Stats(BaseModel):
    tasks_total: int = 0
    tasks_running: int = 0
    tasks_paused: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    assets_total: int = 0
    findings_total: int = 0
    findings: FindingsBySeverity = Field(default_factory=FindingsBySeverity)
    intents_pending: int = 0
    intents_completed: int = 0
    activities_total: int = 0


class UsageStats(BaseModel):
    total: TokenTotal = Field(default_factory=TokenTotal)
    daily: List[DailyTokenBucket] = Field(default_factory=list)
    by_model: List[ModelTokenStat] = Field(default_factory=list)
    by_tool: List[ToolStat] = Field(default_factory=list)


# ==================== Exploration Graph (对齐 ARTEX TaskNode / Edge) ====================

class GraphNode(BaseModel):
    id: str
    node_type: str
    title: str
    confidence: int = 0
    is_verified: int = 0
    depth_level: int = 0


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    anchor_type: str
    weight: int = 1


class GraphData(BaseModel):
    task_id: str
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)


# ==================== Re-export for type hints ====================
ExplorationNodeOut.model_rebuild()
AnchorOut.model_rebuild()
