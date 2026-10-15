from datetime import datetime
from enum import Enum as PyEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint,
    func, Table
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship

from src.db.base import Base


def uuid_default() -> UUID:
    return uuid4()


class TimestampMixin:
    @declared_attr
    def created_at(cls):
        return Column(DateTime, default=func.now(), nullable=False, server_default=func.now())

    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime,
            default=func.now(),
            onupdate=func.now(),
            nullable=False,
            server_default=func.now()
        )


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    name = Column(String(255), nullable=False, index=True)
    domain = Column(String(255), unique=True, nullable=True, index=True)
    industry = Column(String(100), nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    metadata_json = Column(JSONB, default=dict)
    is_active = Column(Integer, default=1, nullable=False)

    assets = relationship("Asset", back_populates="company", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="company", cascade="all, delete-orphan")


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("company_id", "ip_address", name="uq_asset_company_ip"),
        UniqueConstraint("company_id", "domain", name="uq_asset_company_domain"),
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    company_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    asset_type = Column(Enum("domain", "ip", "service", "endpoint", name="asset_type"), nullable=False, index=True)
    ip_address = Column(String(45), nullable=True, index=True)
    domain = Column(String(255), nullable=True, index=True)
    port = Column(Integer, nullable=True)
    service_name = Column(String(100), nullable=True)
    protocol = Column(String(20), nullable=True)
    os_guess = Column(String(100), nullable=True)
    risk_level = Column(Enum("critical", "high", "medium", "low", "info", name="risk_level"), nullable=True)
    tags = Column(JSONB, default=list)
    extra_data = Column(JSONB, default=dict)
    last_seen_at = Column(DateTime, nullable=True)
    is_active = Column(Integer, default=1, nullable=False)

    company = relationship("Company", back_populates="assets")
    tasks = relationship("Task", back_populates="asset", cascade="all, delete-orphan")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    company_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    asset_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    task_type = Column(
        Enum("recon", "scan", "exploit", "post_exploit", "full", name="task_type"),
        nullable=False,
        index=True
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        Enum("pending", "running", "paused", "completed", "failed", "cancelled", name="task_status"),
        default="pending",
        nullable=False,
        index=True
    )
    priority = Column(Integer, default=0)
    max_iterations = Column(Integer, nullable=True)
    current_iteration = Column(Integer, default=0)
    agent_config = Column(JSONB, default=dict)
    context_data = Column(JSONB, default=dict)
    result_summary = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    planner_state_json = Column(JSONB, default=dict)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    company = relationship("Company", back_populates="tasks")
    asset = relationship("Asset", back_populates="tasks")
    exploration_nodes = relationship("ExplorationNode", back_populates="task", cascade="all, delete-orphan")
    activities = relationship("Activity", back_populates="task", cascade="all, delete-orphan")
    intents = relationship("Intent", back_populates="task", cascade="all, delete-orphan")
    findings = relationship("Finding", back_populates="task", cascade="all, delete-orphan")
    goals = relationship("Goal", back_populates="task", cascade="all, delete-orphan")


class ExplorationNodeType:
    goal = "goal"
    intent = "intent"
    fact = "fact"
    finding = "finding"
    clue = "clue"
    tool_output = "tool_output"
    hypothesis = "hypothesis"
    action = "action"


exploration_node_types = Enum(
    ExplorationNodeType.goal,
    ExplorationNodeType.intent,
    ExplorationNodeType.fact,
    ExplorationNodeType.finding,
    ExplorationNodeType.clue,
    ExplorationNodeType.tool_output,
    ExplorationNodeType.hypothesis,
    ExplorationNodeType.action,
    name="exploration_node_type"
)


class ExplorationNode(Base, TimestampMixin):
    __tablename__ = "exploration_nodes"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    task_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    node_type = Column(exploration_node_types, nullable=False, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=True)
    confidence = Column(Integer, default=0)
    source = Column(String(100), nullable=True)
    tool_name = Column(String(100), nullable=True)
    extra_data = Column(JSONB, default=dict)
    is_verified = Column(Integer, default=0, nullable=False)
    depth_level = Column(Integer, default=0)

    task = relationship("Task", back_populates="exploration_nodes")
    anchors_out = relationship(
        "ExplorationAnchor",
        foreign_keys="ExplorationAnchor.from_node_id",
        back_populates="from_node",
        cascade="all, delete-orphan"
    )
    anchors_in = relationship(
        "ExplorationAnchor",
        foreign_keys="ExplorationAnchor.to_node_id",
        back_populates="to_node",
        cascade="all, delete-orphan"
    )


class AnchorTypeEnum(str, Enum):
    supports = "supports"
    contradicts = "contradicts"
    leads_to = "leads_to"
    related_to = "related_to"
    question_for = "question_for"


anchor_types = Enum(
    AnchorTypeEnum.supports,
    AnchorTypeEnum.contradicts,
    AnchorTypeEnum.leads_to,
    AnchorTypeEnum.related_to,
    AnchorTypeEnum.question_for,
    name="anchor_type"
)


class ExplorationAnchor(Base):
    __tablename__ = "exploration_anchors"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    from_node_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("exploration_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    to_node_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("exploration_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    anchor_type = Column(anchor_types, nullable=False)
    weight = Column(Integer, default=1)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, server_default=func.now())

    from_node = relationship("ExplorationNode", foreign_keys=[from_node_id], back_populates="anchors_out")
    to_node = relationship("ExplorationNode", foreign_keys=[to_node_id], back_populates="anchors_in")

    __table_args__ = (
        UniqueConstraint("from_node_id", "to_node_id", name="uq_anchor_pair"),
    )


class ActivityType:
    tool_call = "tool_call"
    tool_result = "tool_result"
    agent_thought = "agent_thought"
    state_update = "state_update"
    milestone = "milestone"
    error = "error"
    human_intervention = "human_intervention"


activity_types = Enum(
    ActivityType.tool_call,
    ActivityType.tool_result,
    ActivityType.agent_thought,
    ActivityType.state_update,
    ActivityType.milestone,
    ActivityType.error,
    ActivityType.human_intervention,
    name="activity_type"
)


class Activity(Base, TimestampMixin):
    __tablename__ = "activities"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    task_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    intent_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("exploration_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    activity_type = Column(activity_types, nullable=False, index=True)
    step_number = Column(Integer, default=0)
    sequence_id = Column(String(50), nullable=True)
    tool_name = Column(String(100), nullable=True)
    tool_input = Column(JSONB, nullable=True)
    tool_output = Column(JSONB, nullable=True)
    agent_thought = Column(Text, nullable=True)
    result_data = Column(JSONB, nullable=True)
    metadata_json = Column(JSONB, default=dict)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    task = relationship("Task", back_populates="activities")
    intent = relationship("ExplorationNode", foreign_keys=[intent_id])


# ------------------------------------------------------------------
# Intent (distributed atomic claim target)
# ------------------------------------------------------------------

class IntentStatus(str, PyEnum):
    pending = "pending"
    claimed = "claimed"
    completed = "completed"
    failed = "failed"
    expired = "expired"


intent_status_enum = Enum(
    "pending", "claimed", "completed", "failed", "expired",
    name="intent_status"
)


class Intent(Base, TimestampMixin):
    """
    Shared todolist item persisted in DB for distributed atomic claiming.
    Workers use SELECT FOR UPDATE SKIP LOCKED to atomically claim one intent.
    """
    __tablename__ = "intents"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    task_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    intent_type = Column(String(50), nullable=False, default="investigation")
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    prerequisite_node_ids = Column(JSONB, default=list)
    priority = Column(Integer, default=0)
    status = Column(intent_status_enum, default=IntentStatus.pending, nullable=False, index=True)
    claimed_by_worker_id = Column(String(100), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    result_node_id = Column(PG_UUID(as_uuid=True), nullable=True)
    error_message = Column(Text, nullable=True)
    extra_data = Column(JSONB, default=dict)


    task = relationship("Task", back_populates="intents")


# ------------------------------------------------------------------
# Notification System
# ------------------------------------------------------------------

class NotificationChannelType(str, PyEnum):
    dingtalk = "dingtalk"
    feishu = "feishu"
    wecom = "wecom"
    webhook = "webhook"
    telegram = "telegram"
    email = "email"


notification_channel_types = Enum(
    NotificationChannelType.dingtalk,
    NotificationChannelType.feishu,
    NotificationChannelType.wecom,
    NotificationChannelType.webhook,
    NotificationChannelType.telegram,
    NotificationChannelType.email,
    name="notification_channel_type"
)


class NotificationEventStatus(str, PyEnum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    retrying = "retrying"


notification_event_status_enum = Enum(
    NotificationEventStatus.pending,
    NotificationEventStatus.sending,
    NotificationEventStatus.sent,
    NotificationEventStatus.failed,
    NotificationEventStatus.retrying,
    name="notification_event_status"
)


class NotificationChannel(Base, TimestampMixin):
    """Registered notification channel with encrypted credentials."""
    __tablename__ = "notification_channels"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    channel_type = Column(notification_channel_types, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    config_encrypted = Column(JSONB, nullable=False)
    enabled = Column(Integer, default=1, nullable=False)
    rate_limit = Column(Integer, default=10, nullable=False)
    metadata_json = Column(JSONB, default=dict)


class NotificationEvent(Base, TimestampMixin):
    """Individual notification event with retry tracking."""
    __tablename__ = "notification_events"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    finding_id = Column(PG_UUID(as_uuid=True), nullable=True, index=True)
    channel_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    message_title = Column(String(255), nullable=False)
    message_body = Column(Text, nullable=True)
    urgency = Column(String(20), default="normal")
    status = Column(notification_event_status_enum, default=NotificationEventStatus.pending, nullable=False, index=True)
    attempt_count = Column(Integer, default=0)
    next_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    metadata_json = Column(JSONB, default=dict)


# ------------------------------------------------------------------
# Findings (漏洞) — 对齐 ARTEX findings {critical,high,medium,low}
# ------------------------------------------------------------------

class Severity(str, PyEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


severity_enum = Enum(
    Severity.critical,
    Severity.high,
    Severity.medium,
    Severity.low,
    Severity.info,
    name="severity",
)


class FindingStatus(str, PyEnum):
    candidate = "candidate"   # 待确认
    confirmed = "confirmed"   # 已确认
    false_positive = "false_positive"
    fixed = "fixed"
    retesting = "retesting"


finding_status_enum = Enum(
    FindingStatus.candidate,
    FindingStatus.confirmed,
    FindingStatus.false_positive,
    FindingStatus.fixed,
    FindingStatus.retesting,
    name="finding_status",
)


class Finding(Base, TimestampMixin):
    """A vulnerability discovered during a task's exploration."""
    __tablename__ = "findings"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    task_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    severity = Column(severity_enum, default=Severity.medium, nullable=False, index=True)
    status = Column(finding_status_enum, default=FindingStatus.candidate, nullable=False, index=True)
    vuln_type = Column(String(100), nullable=True)  # e.g. sqli, idor, rce
    affected_asset = Column(String(500), nullable=True)  # url / host
    cvss_score = Column(Integer, nullable=True)  # 0-100 scaled
    evidence = Column(Text, nullable=True)  # request/response excerpt
    remediation = Column(Text, nullable=True)
    source_node_id = Column(PG_UUID(as_uuid=True), nullable=True)  # originating exploration node
    confidence = Column(Integer, default=0)
    extra_data = Column(JSONB, default=dict)

    task = relationship("Task", back_populates="findings")


# ------------------------------------------------------------------
# Goals (目标) — 对齐 ARTEX goals_total / goals_met
# ------------------------------------------------------------------

class GoalStatus(str, PyEnum):
    pending = "pending"
    in_progress = "in_progress"
    met = "met"
    failed = "failed"


goal_status_enum = Enum(
    GoalStatus.pending,
    GoalStatus.in_progress,
    GoalStatus.met,
    GoalStatus.failed,
    name="goal_status",
)


class Goal(Base, TimestampMixin):
    """A task objective whose completion the engine tracks (prove_goal)."""
    __tablename__ = "goals"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid_default)
    task_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    acceptance_criteria = Column(Text, nullable=True)  # 量化验收标准
    status = Column(goal_status_enum, default=GoalStatus.pending, nullable=False, index=True)
    priority = Column(Integer, default=0)
    met_at = Column(DateTime, nullable=True)
    extra_data = Column(JSONB, default=dict)

    task = relationship("Task", back_populates="goals")

