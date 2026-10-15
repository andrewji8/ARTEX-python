import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Tuple, Type, TypeVar
from uuid import UUID

from sqlalchemy import (
    Row,
    Select,
    and_,
    column,
    delete,
    insert,
    select,
    update,
    func,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from src.db.models import (
    Activity,
    ActivityType,
    AnchorTypeEnum,
    Asset,
    Company,
    ExplorationAnchor,
    ExplorationNode,
    ExplorationNodeType,
    Finding,
    FindingStatus,
    Goal,
    GoalStatus,
    Intent,
    IntentStatus,
    Severity,
    Task,
)
from src.db.schemas import (
    ActivityCreate,
    ActivityOut,
    AnchorCreate,
    AnchorOut,
    AssetOut,
    AssetUpsertPayload,
    CompanyOut,
    DailyTokenBucket,
    FindingCreate,
    FindingOut,
    FindingUpdate,
    GoalCreate,
    GoalOut,
    GoalUpdate,
    GraphData,
    GraphEdge,
    GraphNode,
    ModelTokenStat,
    Stats,
    ToolStat,
    UsageStats,
    ExplorationNodeOut,
    TaskContextData,
    TaskOut,
)

T = TypeVar("T")


def _node_to_out(node: ExplorationNode, anchors: List[ExplorationAnchor]) -> ExplorationNodeOut:
    return ExplorationNodeOut(
        id=node.id,
        task_id=node.task_id,
        node_type=node.node_type,
        title=node.title,
        content=node.content,
        confidence=node.confidence,
        source=node.source,
        tool_name=node.tool_name,
        extra_data=node.extra_data or {},
        is_verified=node.is_verified,
        depth_level=node.depth_level,
        anchors=[
            AnchorOut(
                id=a.id,
                from_node_id=a.from_node_id,
                to_node_id=a.to_node_id,
                anchor_type=a.anchor_type,
                weight=a.weight,
                notes=a.notes,
                from_node=None,
                to_node=None,
                created_at=a.created_at,
            )
            for a in anchors
        ],
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def _activity_to_out(activity: Activity) -> ActivityOut:
    return ActivityOut(
        id=activity.id,
        task_id=activity.task_id,
        intent_id=activity.intent_id,
        activity_type=activity.activity_type,
        step_number=activity.step_number,
        sequence_id=activity.sequence_id,
        tool_name=activity.tool_name,
        tool_input=activity.tool_input,
        tool_output=activity.tool_output,
        agent_thought=activity.agent_thought,
        result_data=activity.result_data,
        metadata_json=activity.metadata_json or {},
        duration_ms=activity.duration_ms,
        error_message=activity.error_message,
        created_at=activity.created_at,
        updated_at=activity.updated_at,
    )


class AsyncGraphRepository:
    """
    Asynchronous Data Access Layer for the Dual-Graph architecture.
    All methods are fully async and avoid N+1 queries.
    """

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------

    async def create_company(self, name: str, **kwargs) -> CompanyOut:
        company = Company(name=name, **kwargs)
        self.db.add(company)
        await self.db.flush()
        await self.db.refresh(company)
        return self._company_to_out(company)

    async def get_company(self, company_id: UUID) -> Optional[CompanyOut]:
        stmt = select(Company).where(Company.id == company_id)
        result = await self.db.execute(stmt)
        company = result.scalar_one_or_none()
        return self._company_to_out(company) if company else None

    async def list_companies(self, skip: int = 0, limit: int = 100) -> Tuple[List[CompanyOut], int]:
        count_stmt = select(func.count()).select_from(Company)
        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = select(Company).order_by(Company.created_at.desc()).offset(skip).limit(limit)
        result = await self.db.execute(stmt)
        companies = [self._company_to_out(c) for c in result.scalars().all()]
        return companies, total

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    async def upsert_assets(self, company_id: UUID, asset_list: List[AssetUpsertPayload]) -> List[AssetOut]:
        """
        Atomic UPSERT via ON CONFLICT DO UPDATE for both unique constraints:
          - uq_asset_company_ip   (company_id, ip_address)
          - uq_asset_company_domain  (company_id, domain)

        Each payload is attempted on the IP constraint (when ip_address is set)
        and then on the domain constraint (when domain is set).  Results are
        deduplicated by asset id.
        """
        stmt_ip = insert(Asset).returning(Asset.id)
        stmt_domain = insert(Asset).returning(Asset.id)

        if not asset_list:
            return []

        rows_ip: List[Dict[str, Any]] = []
        rows_domain: List[Dict[str, Any]] = []
        ids_with_ip: set[UUID] = set()
        ids_with_domain: set[UUID] = set()

        for payload in asset_list:
            row: Dict[str, Any] = {
                "company_id": company_id,
                "asset_type": payload.asset_type.value,
                "ip_address": payload.ip_address,
                "domain": payload.domain,
                "port": payload.port,
                "service_name": payload.service_name,
                "protocol": payload.protocol,
                "os_guess": payload.os_guess,
                "risk_level": payload.risk_level.value if payload.risk_level else None,
                "tags": payload.tags,
                "extra_data": payload.extra_data,
                "last_seen_at": payload.last_seen_at,
            }
            rows_ip.append(row)
            rows_domain.append(row.copy())
            if payload.ip_address:
                ids_with_ip.add(UUID(row["ip_address"]))
            if payload.domain:
                ids_with_domain.add(UUID(row["domain"]))

        # ── UPSERT on uq_asset_company_ip ──────────────────────────────
        if rows_ip:
            stmt_ip = stmt_ip.on_conflict_do_update(
                index_elements=["company_id", "ip_address"],
                where=Asset.ip_address.isnot(None),
                set_=dict(
                    port=func.coalesce(
                        text("excluded.port"), column("assets.port")
                    ),
                    service_name=func.coalesce(
                        text("excluded.service_name"), column("assets.service_name")
                    ),
                    protocol=func.coalesce(
                        text("excluded.protocol"), column("assets.protocol")
                    ),
                    os_guess=func.coalesce(
                        text("excluded.os_guess"), column("assets.os_guess")
                    ),
                    risk_level=func.coalesce(
                        text("excluded.risk_level"), column("assets.risk_level")
                    ),
                    tags=func.coalesce(
                        text("excluded.tags"), column("assets.tags")
                    ),
                    extra_data=func.coalesce(
                        text("excluded.extra_data"), column("assets.extra_data")
                    ),
                    last_seen_at=func.coalesce(
                        text("excluded.last_seen_at"), column("assets.last_seen_at")
                    ),
                    updated_at=func.now(),
                ),
            )
            result = await self.db.execute(stmt_ip, rows_ip)
            upserted_ids: set[UUID] = {r[0] for r in result.fetchall()}
        else:
            upserted_ids = set()

        # ── UPSERT on uq_asset_company_domain (only for assets with domain) ──
        if rows_domain:
            stmt_domain = stmt_domain.on_conflict_do_update(
                index_elements=["company_id", "domain"],
                where=Asset.domain.isnot(None),
                set_=dict(
                    ip_address=func.coalesce(
                        text("excluded.ip_address"), column("assets.ip_address")
                    ),
                    port=func.coalesce(
                        text("excluded.port"), column("assets.port")
                    ),
                    service_name=func.coalesce(
                        text("excluded.service_name"), column("assets.service_name")
                    ),
                    protocol=func.coalesce(
                        text("excluded.protocol"), column("assets.protocol")
                    ),
                    os_guess=func.coalesce(
                        text("excluded.os_guess"), column("assets.os_guess")
                    ),
                    risk_level=func.coalesce(
                        text("excluded.risk_level"), column("assets.risk_level")
                    ),
                    tags=func.coalesce(
                        text("excluded.tags"), column("assets.tags")
                    ),
                    extra_data=func.coalesce(
                        text("excluded.extra_data"), column("assets.extra_data")
                    ),
                    last_seen_at=func.coalesce(
                        text("excluded.last_seen_at"), column("assets.last_seen_at")
                    ),
                    updated_at=func.now(),
                ),
            )
            result = await self.db.execute(stmt_domain, rows_domain)
            upserted_ids.update(r[0] for r in result.fetchall())

        await self.db.flush()

        if not upserted_ids:
            return []

        stmt = select(Asset).where(Asset.id.in_(upserted_ids))
        result = await self.db.execute(stmt)
        return [self._asset_to_out(a) for a in result.scalars().all()]

    async def get_assets(self, company_id: UUID, asset_type: Optional[str] = None, skip: int = 0, limit: int = 100) -> Tuple[List[AssetOut], int]:
        conditions = [Asset.company_id == company_id, Asset.is_active == 1]
        if asset_type:
            conditions.append(Asset.asset_type == asset_type)

        count_stmt = select(func.count()).select_from(Asset).where(*conditions)
        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = select(Asset).where(*conditions).order_by(Asset.created_at.desc()).offset(skip).limit(limit)
        result = await self.db.execute(stmt)
        return [self._asset_to_out(a) for a in result.scalars().all()], total

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def create_task(self, company_id: UUID, title: str, task_type: str, **kwargs) -> TaskOut:
        task = Task(
            company_id=company_id,
            title=title,
            task_type=task_type,
            **kwargs,
        )
        self.db.add(task)
        await self.db.flush()
        await self.db.refresh(task)
        return self._task_to_out(task)

    async def get_task(self, task_id: UUID) -> Optional[TaskOut]:
        stmt = (
            select(Task)
            .options(selectinload(Task.exploration_nodes), selectinload(Task.activities))
            .where(Task.id == task_id)
        )
        result = await self.db.execute(stmt)
        task = result.scalar_one_or_none()
        return self._task_to_out(task) if task else None

    async def update_task_status(self, task_id: UUID, status: str, **extra) -> Optional[TaskOut]:
        update_data = {"status": status, "updated_at": func.now(), **extra}
        stmt = update(Task).where(Task.id == task_id).values(**update_data)
        await self.db.execute(stmt)
        return await self.get_task(task_id)

    # ------------------------------------------------------------------
    # Exploration Nodes
    # ------------------------------------------------------------------

    async def create_node(self, task_id: UUID, node_type: str, title: str, **kwargs) -> ExplorationNodeOut:
        node = ExplorationNode(
            task_id=task_id,
            node_type=node_type,
            title=title,
            **kwargs,
        )
        self.db.add(node)
        await self.db.flush()
        await self.db.refresh(node)
        return self._node_to_out(node, [])

    async def list_nodes(self, task_id: UUID, node_type: Optional[str] = None, skip: int = 0, limit: int = 100) -> Tuple[List[ExplorationNodeOut], int]:
        conditions = [ExplorationNode.task_id == task_id]
        if node_type:
            conditions.append(ExplorationNode.node_type == node_type)

        count_stmt = select(func.count()).select_from(ExplorationNode).where(*conditions)
        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = (
            select(ExplorationNode)
            .options(
                selectinload(ExplorationNode.anchors_out),
                selectinload(ExplorationNode.anchors_in),
            )
            .where(*conditions)
            .order_by(ExplorationNode.created_at.asc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        nodes = result.scalars().all()

        all_anchors = []
        for n in nodes:
            all_anchors.extend(n.anchors_out or [])
            all_anchors.extend(n.anchors_in or [])

        anchor_map: Dict[UUID, List[ExplorationAnchor]] = {}
        for anchor in all_anchors:
            key = anchor.from_node_id if anchor.from_node_id in [n.id for n in nodes] else anchor.to_node_id
            if anchor.from_node_id in [n.id for n in nodes]:
                anchor_map.setdefault(anchor.from_node_id, []).append(anchor)
            if anchor.to_node_id in [n.id for n in nodes]:
                anchor_map.setdefault(anchor.to_node_id, []).append(anchor)

        return [self._node_to_out(n, anchor_map.get(n.id, [])) for n in nodes], total

    # ------------------------------------------------------------------
    # Anchors
    # ------------------------------------------------------------------

    async def create_anchor(self, anchor_data: AnchorCreate) -> AnchorOut:
        anchor = ExplorationAnchor(
            from_node_id=anchor_data.from_node_id,
            to_node_id=anchor_data.to_node_id,
            anchor_type=anchor_data.anchor_type.value,
            weight=anchor_data.weight or 1,
            notes=anchor_data.notes,
        )
        self.db.add(anchor)
        await self.db.flush()
        await self.db.refresh(anchor)
        return self._anchor_to_out(anchor)

    async def delete_anchor(self, anchor_id: UUID) -> bool:
        stmt = delete(ExplorationAnchor).where(ExplorationAnchor.id == anchor_id)
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Activities (High-throughput)
    # ------------------------------------------------------------------

    async def append_activity(self, task_id: UUID, activity_data: ActivityCreate) -> ActivityOut:
        activity = Activity(
            task_id=task_id,
            intent_id=activity_data.intent_id,
            activity_type=activity_data.activity_type.value,
            step_number=activity_data.step_number or 0,
            sequence_id=activity_data.sequence_id,
            tool_name=activity_data.tool_name,
            tool_input=activity_data.tool_input,
            tool_output=activity_data.tool_output,
            agent_thought=activity_data.agent_thought,
            result_data=activity_data.result_data,
            metadata_json=activity_data.metadata_json or {},
            duration_ms=activity_data.duration_ms,
            error_message=activity_data.error_message,
        )
        self.db.add(activity)
        await self.db.flush()
        await self.db.refresh(activity)
        return self._activity_to_out(activity)

    async def batch_append_activities(self, task_id: UUID, activities: List[ActivityCreate]) -> List[ActivityOut]:
        rows = []
        for act in activities:
            rows.append({
                "task_id": task_id,
                "activity_type": act.activity_type.value,
                "step_number": act.step_number or 0,
                "sequence_id": act.sequence_id,
                "tool_name": act.tool_name,
                "tool_input": act.tool_input,
                "tool_output": act.tool_output,
                "agent_thought": act.agent_thought,
                "result_data": act.result_data,
                "metadata_json": act.metadata_json or {},
                "duration_ms": act.duration_ms,
                "error_message": act.error_message,
                "intent_id": act.intent_id,
            })

        if not rows:
            return []

        stmt = text("""
            INSERT INTO activities
                (id, task_id, intent_id, activity_type, step_number, sequence_id,
                 tool_name, tool_input, tool_output, agent_thought, result_data,
                 metadata_json, duration_ms, error_message, created_at, updated_at)
            VALUES (
                gen_random_uuid(), :task_id, :intent_id, :activity_type, :step_number,
                :sequence_id, :tool_name, :tool_input, :tool_output, :agent_thought,
                :result_data, :metadata_json, :duration_ms, :error_message,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """)

        await self.db.execute(stmt, rows)
        await self.db.flush()
        return [self._activity_to_out(r[0]) for r in await self._get_activities_by_ids([row["id"] for row in rows])] if False else []

    async def _get_activities_by_ids(self, ids: List[UUID]) -> List[Activity]:
        stmt = select(Activity).where(Activity.id.in_(ids))
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def list_activities(self, task_id: UUID, skip: int = 0, limit: int = 100) -> Tuple[List[ActivityOut], int]:
        count_stmt = select(func.count()).select_from(Activity).where(Activity.task_id == task_id)
        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = (
            select(Activity)
            .where(Activity.task_id == task_id)
            .order_by(Activity.created_at.asc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [self._activity_to_out(a) for a in result.scalars().all()], total

    # ------------------------------------------------------------------
    # Task Context (Eager Loading)
    # ------------------------------------------------------------------

    async def get_task_context(self, task_id: UUID) -> Optional[TaskContextData]:
        task = await self.get_task(task_id)
        if not task:
            return None

        nodes_stmt = (
            select(ExplorationNode)
            .options(
                selectinload(ExplorationNode.anchors_out),
                selectinload(ExplorationNode.anchors_in),
            )
            .where(ExplorationNode.task_id == task_id)
            .order_by(ExplorationNode.created_at.asc())
        )
        nodes_result = await self.db.execute(nodes_stmt)
        nodes = nodes_result.scalars().all()

        all_anchors: List[ExplorationAnchor] = []
        for node in nodes:
            if node.anchors_out:
                all_anchors.extend(node.anchors_out)
            if node.anchors_in:
                all_anchors.extend(node.anchors_in)

        anchor_map: Dict[UUID, List[ExplorationAnchor]] = {}
        for anchor in all_anchors:
            anchor_map.setdefault(anchor.from_node_id, []).append(anchor)
            anchor_map.setdefault(anchor.to_node_id, []).append(anchor)

        nodes_out = [self._node_to_out(n, anchor_map.get(n.id, [])) for n in nodes]

        anchors_out = [
            AnchorOut(
                id=a.id,
                from_node_id=a.from_node_id,
                to_node_id=a.to_node_id,
                anchor_type=a.anchor_type,
                weight=a.weight,
                notes=a.notes,
                from_node=None,
                to_node=None,
                created_at=a.created_at,
            )
            for a in all_anchors
        ]

        return TaskContextData(
            task=task,
            nodes=nodes_out,
            anchors=anchors_out,
            activities=[],
            total_nodes=len(nodes_out),
            total_anchors=len(anchors_out),
            total_activities=0,
        )

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _company_to_out(self, company: Company) -> CompanyOut:
        return CompanyOut(
            id=company.id,
            name=company.name,
            domain=company.domain,
            industry=company.industry,
            contact_email=company.contact_email,
            contact_phone=company.contact_phone,
            address=company.address,
            metadata_json=company.metadata_json or {},
            is_active=company.is_active,
            created_at=company.created_at,
            updated_at=company.updated_at,
        )

    def _asset_to_out(self, asset: Asset) -> AssetOut:
        return AssetOut(
            id=asset.id,
            company_id=asset.company_id,
            asset_type=asset.asset_type,
            ip_address=asset.ip_address,
            domain=asset.domain,
            port=asset.port,
            service_name=asset.service_name,
            protocol=asset.protocol,
            os_guess=asset.os_guess,
            risk_level=asset.risk_level,
            tags=asset.tags or [],
            extra_data=asset.extra_data or {},
            last_seen_at=asset.last_seen_at,
            is_active=asset.is_active,
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )

    def _task_to_out(self, task: Task) -> TaskOut:
        return TaskOut(
            id=task.id,
            company_id=task.company_id,
            asset_id=task.asset_id,
            task_type=task.task_type,
            title=task.title,
            description=task.description,
            status=task.status,
            priority=task.priority,
            max_iterations=task.max_iterations,
            current_iteration=task.current_iteration,
            agent_config=task.agent_config or {},
            context_data=task.context_data or {},
            result_summary=task.result_summary,
            error_message=task.error_message,
            started_at=task.started_at,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    def _node_to_out(self, node: ExplorationNode, anchors: List[ExplorationAnchor]) -> ExplorationNodeOut:
        return ExplorationNodeOut(
            id=node.id,
            task_id=node.task_id,
            node_type=node.node_type,
            title=node.title,
            content=node.content,
            confidence=node.confidence,
            source=node.source,
            tool_name=node.tool_name,
            extra_data=node.extra_data or {},
            is_verified=node.is_verified,
            depth_level=node.depth_level,
            anchors=[
                AnchorOut(
                    id=a.id,
                    from_node_id=a.from_node_id,
                    to_node_id=a.to_node_id,
                    anchor_type=a.anchor_type,
                    weight=a.weight,
                    notes=a.notes,
                    from_node=None,
                    to_node=None,
                    created_at=a.created_at,
                )
                for a in anchors
            ],
            created_at=node.created_at,
            updated_at=node.updated_at,
        )

    def _anchor_to_out(self, anchor: ExplorationAnchor) -> AnchorOut:
        return AnchorOut(
            id=anchor.id,
            from_node_id=anchor.from_node_id,
            to_node_id=anchor.to_node_id,
            anchor_type=anchor.anchor_type,
            weight=anchor.weight,
            notes=anchor.notes,
            from_node=None,
            to_node=None,
            created_at=anchor.created_at,
        )

    def _activity_to_out(self, activity: Activity) -> ActivityOut:
        return ActivityOut(
            id=activity.id,
            task_id=activity.task_id,
            intent_id=activity.intent_id,
            activity_type=activity.activity_type,
            step_number=activity.step_number,
            sequence_id=activity.sequence_id,
            tool_name=activity.tool_name,
            tool_input=activity.tool_input,
            tool_output=activity.tool_output,
            agent_thought=activity.agent_thought,
            result_data=activity.result_data,
            metadata_json=activity.metadata_json or {},
            duration_ms=activity.duration_ms,
            error_message=activity.error_message,
            created_at=activity.created_at,
            updated_at=activity.updated_at,
        )

    # ------------------------------------------------------------------
    # Intent — distributed atomic claim via PostgreSQL SKIP LOCKED
    # ------------------------------------------------------------------

    async def create_intent(self, task_id: UUID, *, title: str, description: str = "",
                            prerequisite_node_ids: List[UUID] | None = None,
                            priority: int = 0, intent_type: str = "investigation",
                            extra_data: Dict[str, Any] | None = None) -> Intent:
        """Create a pending intent for the given task."""
        intent = Intent(
            task_id=task_id,
            intent_type=intent_type,
            title=title,
            description=description,
            prerequisite_node_ids=prerequisite_node_ids or [],
            priority=priority,
            extra_data=extra_data or {},
        )
        self.db.add(intent)
        await self.db.flush()
        await self.db.refresh(intent)
        return intent

    async def batch_create_intents(self, task_id: UUID,
                                   intents_data: List[Dict[str, Any]]) -> List[Intent]:
        """Bulk-create pending intents."""
        if not intents_data:
            return []
        rows = [
            {
                "id": uuid4(),
                "task_id": task_id,
                "intent_type": d.get("intent_type", "investigation"),
                "title": d["title"],
                "description": d.get("description", ""),
                "prerequisite_node_ids": d.get("prerequisite_node_ids", []),
                "priority": d.get("priority", 0),
                "extra_data": d.get("extra_data", {}),
            }
            for d in intents_data
        ]
        stmt = insert(Intent).values(rows)
        await self.db.execute(stmt)
        await self.db.flush()
        stmt_out = select(Intent).where(Intent.task_id == task_id).order_by(Intent.priority.desc())
        result = await self.db.execute(stmt_out)
        return list(result.scalars().all())

    async def claim_intent(self, task_id: UUID, worker_id: str) -> Optional[Intent]:
        """
        Atomic claim via SELECT … FOR UPDATE SKIP LOCKED.
        Works correctly under uvicorn --workers N and across containers.
        """
        stmt = (
            select(Intent)
            .where(
                Intent.task_id == task_id,
                Intent.status == IntentStatus.pending,
            )
            .order_by(Intent.priority.desc(), Intent.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        result = await self.db.execute(stmt)
        intent = result.scalar_one_or_none()
        if intent is None:
            return None
        intent.status = IntentStatus.claimed
        intent.claimed_by_worker_id = worker_id
        intent.claimed_at = func.now()
        await self.db.flush()
        await self.db.refresh(intent)
        return intent

    async def create_activity(
        self,
        *,
        task_id: UUID,
        intent_id: Optional[UUID] = None,
        activity_type: str,
        tool_name: Optional[str] = None,
        tool_input: Optional[Dict[str, Any]] = None,
        tool_output: Optional[Dict[str, Any]] = None,
        agent_thought: Optional[str] = None,
        result_data: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
        metadata_json: Optional[Dict[str, Any]] = None,
        step_number: int = 0,
    ) -> "Activity":
        """Create and persist an activity record (used by workers)."""
        act = Activity(
            task_id=task_id,
            intent_id=intent_id,
            activity_type=activity_type,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            agent_thought=agent_thought,
            result_data=result_data,
            duration_ms=duration_ms,
            metadata_json=metadata_json or {},
            step_number=step_number,
        )
        self.db.add(act)
        await self.db.flush()
        await self.db.refresh(act)
        return act

    async def complete_intent(self, intent_id: UUID, *, result_node_id: UUID | None = None,
                              error_message: str | None = None) -> Optional[Intent]:
        """Mark an intent as completed."""
        stmt = (
            update(Intent)
            .where(Intent.id == intent_id, Intent.status == IntentStatus.claimed)
            .values(
                status=IntentStatus.completed,
                result_node_id=result_node_id,
                error_message=error_message,
                completed_at=func.now(),
            )
        )
        await self.db.execute(stmt)
        await self.db.flush()
        return await self.get_intent(intent_id)

    async def fail_intent(self, intent_id: UUID, *, error_message: str) -> Optional[Intent]:
        """Mark an intent as failed."""
        stmt = (
            update(Intent)
            .where(Intent.id == intent_id, Intent.status == IntentStatus.claimed)
            .values(
                status=IntentStatus.failed,
                error_message=error_message,
                completed_at=func.now(),
            )
        )
        await self.db.execute(stmt)
        await self.db.flush()
        return await self.get_intent(intent_id)

    async def release_intent(self, intent_id: UUID) -> Optional[Intent]:
        """Release a claimed intent back to pending (worker timeout / abort)."""
        stmt = (
            update(Intent)
            .where(Intent.id == intent_id, Intent.status == IntentStatus.claimed)
            .values(
                status=IntentStatus.pending,
                claimed_by_worker_id=None,
                claimed_at=None,
            )
        )
        await self.db.execute(stmt)
        await self.db.flush()
        return await self.get_intent(intent_id)

    async def get_intent(self, intent_id: UUID) -> Optional[Intent]:
        stmt = select(Intent).where(Intent.id == intent_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_pending_intents(self, task_id: UUID, limit: int = 50) -> List[Intent]:
        stmt = (
            select(Intent)
            .where(Intent.task_id == task_id, Intent.status == IntentStatus.pending)
            .order_by(Intent.priority.desc(), Intent.created_at.asc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_by_status(self, task_id: UUID) -> Dict[str, int]:
        """Return counts per status for monitoring."""
        stmt = (
            select(Intent.status, func.count())
            .where(Intent.task_id == task_id)
            .group_by(Intent.status)
        )
        result = await self.db.execute(stmt)
        return {row[0].value: row[1] for row in result.all()}

    # ------------------------------------------------------------------
    # PlannerState persistence (Fix #2)
    # ------------------------------------------------------------------

    async def save_planner_state(self, task_id: UUID, state: Dict[str, Any]) -> None:
        """Persist the PlannerState to tasks.planner_state_json."""
        stmt = (
            update(Task)
            .where(Task.id == task_id)
            .values(planner_state_json=state)
        )
        await self.db.execute(stmt)
        await self.db.flush()

    async def load_planner_state(self, task_id: UUID) -> Dict[str, Any]:
        """Load persisted PlannerState from DB; returns empty dict if none."""
        stmt = select(Task.planner_state_json).where(Task.id == task_id)
        result = await self.db.execute(stmt)
        value = result.scalar_one_or_none()
        return value or {}

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    async def create_finding(self, task_id: UUID, data: FindingCreate) -> FindingOut:
        finding = Finding(
            task_id=task_id,
            title=data.title,
            description=data.description,
            severity=data.severity.value,
            status=data.status.value,
            vuln_type=data.vuln_type,
            affected_asset=data.affected_asset,
            cvss_score=data.cvss_score,
            evidence=data.evidence,
            remediation=data.remediation,
            source_node_id=data.source_node_id,
            confidence=data.confidence or 0,
            extra_data=data.extra_data or {},
        )
        self.db.add(finding)
        await self.db.flush()
        await self.db.refresh(finding)
        return self._finding_to_out(finding)

    async def get_finding(self, finding_id: UUID) -> Optional[FindingOut]:
        stmt = select(Finding).where(Finding.id == finding_id)
        result = await self.db.execute(stmt)
        finding = result.scalar_one_or_none()
        return self._finding_to_out(finding) if finding else None

    async def list_findings(
        self,
        task_id: UUID,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[List[FindingOut], int]:
        conditions = [Finding.task_id == task_id]
        if severity:
            conditions.append(Finding.severity == severity)
        if status:
            conditions.append(Finding.status == status)

        count_stmt = select(func.count()).select_from(Finding).where(*conditions)
        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = (
            select(Finding)
            .where(*conditions)
            .order_by(Finding.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [self._finding_to_out(f) for f in result.scalars().all()], total

    async def update_finding(self, finding_id: UUID, data: FindingUpdate) -> Optional[FindingOut]:
        stmt = select(Finding).where(Finding.id == finding_id)
        result = await self.db.execute(stmt)
        finding = result.scalar_one_or_none()
        if not finding:
            return None
        update_data = data.model_dump(exclude_unset=True, exclude_none=True)
        for key, value in update_data.items():
            if hasattr(value, "value"):  # Enum -> str
                value = value.value
            setattr(finding, key, value)
        await self.db.flush()
        await self.db.refresh(finding)
        return self._finding_to_out(finding)

    async def delete_finding(self, finding_id: UUID) -> bool:
        stmt = delete(Finding).where(Finding.id == finding_id)
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.rowcount > 0

    async def findings_by_severity(self, task_id: Optional[UUID] = None) -> Dict[str, int]:
        """Return {critical, high, medium, low} counts (optionally scoped to a task)."""
        conditions = []
        if task_id is not None:
            conditions.append(Finding.task_id == task_id)
        stmt = select(Finding.severity, func.count()).group_by(Finding.severity)
        if conditions:
            stmt = stmt.where(*conditions)
        result = await self.db.execute(stmt)
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for severity, count in result.all():
            if severity in counts:
                counts[severity] = count
        return counts

    def _finding_to_out(self, finding: Finding) -> FindingOut:
        return FindingOut(
            id=finding.id,
            task_id=finding.task_id,
            title=finding.title,
            description=finding.description,
            severity=finding.severity,
            status=finding.status,
            vuln_type=finding.vuln_type,
            affected_asset=finding.affected_asset,
            cvss_score=finding.cvss_score,
            evidence=finding.evidence,
            remediation=finding.remediation,
            source_node_id=finding.source_node_id,
            confidence=finding.confidence or 0,
            extra_data=finding.extra_data or {},
            created_at=finding.created_at,
            updated_at=finding.updated_at,
        )

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    async def create_goal(self, task_id: UUID, data: GoalCreate) -> GoalOut:
        goal = Goal(
            task_id=task_id,
            title=data.title,
            description=data.description,
            acceptance_criteria=data.acceptance_criteria,
            status=data.status.value,
            priority=data.priority or 0,
            extra_data=data.extra_data or {},
        )
        self.db.add(goal)
        await self.db.flush()
        await self.db.refresh(goal)
        return self._goal_to_out(goal)

    async def list_goals(self, task_id: UUID) -> List[GoalOut]:
        stmt = select(Goal).where(Goal.task_id == task_id).order_by(Goal.priority.desc(), Goal.created_at.asc())
        result = await self.db.execute(stmt)
        return [self._goal_to_out(g) for g in result.scalars().all()]

    async def get_goal(self, goal_id: UUID) -> Optional[GoalOut]:
        stmt = select(Goal).where(Goal.id == goal_id)
        result = await self.db.execute(stmt)
        goal = result.scalar_one_or_none()
        return self._goal_to_out(goal) if goal else None

    async def update_goal(self, goal_id: UUID, data: GoalUpdate) -> Optional[GoalOut]:
        stmt = select(Goal).where(Goal.id == goal_id)
        result = await self.db.execute(stmt)
        goal = result.scalar_one_or_none()
        if not goal:
            return None
        update_data = data.model_dump(exclude_unset=True, exclude_none=True)
        for key, value in update_data.items():
            if hasattr(value, "value"):
                value = value.value
            setattr(goal, key, value)
        if update_data.get("status") == GoalStatus.met.value:
            goal.met_at = func.now()
        await self.db.flush()
        await self.db.refresh(goal)
        return self._goal_to_out(goal)

    async def delete_goal(self, goal_id: UUID) -> bool:
        stmt = delete(Goal).where(Goal.id == goal_id)
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.rowcount > 0

    async def goal_counts(self, task_id: UUID) -> Tuple[int, int]:
        """Return (goals_total, goals_met) for a task."""
        total_stmt = select(func.count()).select_from(Goal).where(Goal.task_id == task_id)
        total = (await self.db.execute(total_stmt)).scalar_one()
        met_stmt = (
            select(func.count())
            .select_from(Goal)
            .where(Goal.task_id == task_id, Goal.status == GoalStatus.met.value)
        )
        met = (await self.db.execute(met_stmt)).scalar_one()
        return total, met

    def _goal_to_out(self, goal: Goal) -> GoalOut:
        return GoalOut(
            id=goal.id,
            task_id=goal.task_id,
            title=goal.title,
            description=goal.description,
            acceptance_criteria=goal.acceptance_criteria,
            status=goal.status,
            priority=goal.priority or 0,
            met_at=goal.met_at,
            extra_data=goal.extra_data or {},
            created_at=goal.created_at,
            updated_at=goal.updated_at,
        )

    # ------------------------------------------------------------------
    # Dashboard stats / usage / graph
    # ------------------------------------------------------------------

    async def get_stats(self) -> Stats:
        """Aggregate dashboard overview statistics across all tasks."""
        task_status_stmt = select(Task.status, func.count()).group_by(Task.status)
        task_counts = {status: count for status, count in (await self.db.execute(task_status_stmt)).all()}

        asset_total = (await self.db.execute(select(func.count()).select_from(Asset))).scalar_one()
        findings_total = (await self.db.execute(select(func.count()).select_from(Finding))).scalar_one()
        activities_total = (await self.db.execute(select(func.count()).select_from(Activity))).scalar_one()

        intent_counts = await self._intent_status_counts(None)
        sev = await self.findings_by_severity()

        return Stats(
            tasks_total=sum(task_counts.values()),
            tasks_running=task_counts.get("running", 0),
            tasks_paused=task_counts.get("paused", 0),
            tasks_done=task_counts.get("completed", 0) + task_counts.get("done", 0),
            tasks_failed=task_counts.get("failed", 0),
            assets_total=asset_total,
            findings_total=findings_total,
            findings=sev,  # type: ignore[arg-type]
            intents_pending=intent_counts.get("pending", 0) + intent_counts.get("claimed", 0),
            intents_completed=intent_counts.get("completed", 0),
            activities_total=activities_total,
        )

    async def _intent_status_counts(self, task_id: Optional[UUID]) -> Dict[str, int]:
        stmt = select(Intent.status, func.count()).group_by(Intent.status)
        if task_id is not None:
            stmt = stmt.where(Intent.task_id == task_id)
        result = await self.db.execute(stmt)
        return {status: count for status, count in result.all()}

    async def get_usage(self) -> UsageStats:
        """
        Aggregate token usage from Activity.result_data where a worker recorded
        token counts (input/output/cache_read/cache_write).
        """
        total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        daily: Dict[str, Dict[str, int]] = {}
        by_model: Dict[str, Dict[str, int]] = {}

        stmt = select(Activity).where(Activity.result_data.isnot(None))
        result = await self.db.execute(stmt)
        for activity in result.scalars().all():
            data = activity.result_data or {}
            usage = data.get("token_usage") or data
            inp = int(usage.get("input_tokens", 0) or 0)
            out = int(usage.get("output_tokens", 0) or 0)
            cr = int(usage.get("cache_read_tokens", 0) or 0)
            cw = int(usage.get("cache_write_tokens", 0) or 0)
            if not any((inp, out, cr, cw)):
                continue
            total["input_tokens"] += inp
            total["output_tokens"] += out
            total["cache_read_tokens"] += cr
            total["cache_write_tokens"] += cw

            day = activity.created_at.strftime("%Y-%m-%d") if activity.created_at else "unknown"
            bucket = daily.setdefault(day, {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0})
            bucket["input_tokens"] += inp
            bucket["output_tokens"] += out
            bucket["cache_read_tokens"] += cr
            bucket["cache_write_tokens"] += cw

            model = activity.tool_name or "unknown"
            m = by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0})
            m["calls"] += 1
            m["input_tokens"] += inp
            m["output_tokens"] += out
            m["cache_read_tokens"] += cr
            m["cache_write_tokens"] += cw

        tool_stmt = (
            select(Activity.tool_name, func.count())
            .where(Activity.tool_name.isnot(None))
            .group_by(Activity.tool_name)
            .order_by(func.count().desc())
            .limit(20)
        )
        by_tool = {tool: count for tool, count in (await self.db.execute(tool_stmt)).all()}

        return UsageStats(
            total=total,  # type: ignore[arg-type]
            daily=[DailyTokenBucket(date=d, **v) for d, v in sorted(daily.items())],
            by_model=[ModelTokenStat(model=m, **v) for m, v in sorted(by_model.items(), key=lambda kv: kv[1]["calls"], reverse=True)],
            by_tool=[ToolStat(tool=t, calls=c) for t, c in by_tool.items()],
        )

    async def get_graph(self, task_id: UUID) -> GraphData:
        """Return exploration nodes + anchor edges for graph visualization."""
        nodes_stmt = (
            select(ExplorationNode)
            .where(ExplorationNode.task_id == task_id)
            .order_by(ExplorationNode.created_at.asc())
        )
        nodes = (await self.db.execute(nodes_stmt)).scalars().all()
        node_ids = {n.id for n in nodes}

        anchors_stmt = select(ExplorationAnchor).where(
            ExplorationAnchor.from_node_id.in_(node_ids),
            ExplorationAnchor.to_node_id.in_(node_ids),
        )
        anchors = (await self.db.execute(anchors_stmt)).scalars().all()

        return GraphData(
            task_id=str(task_id),
            nodes=[
                GraphNode(
                    id=str(n.id),
                    node_type=n.node_type,
                    title=n.title,
                    confidence=n.confidence or 0,
                    is_verified=n.is_verified or 0,
                    depth_level=n.depth_level or 0,
                )
                for n in nodes
            ],
            edges=[
                GraphEdge(
                    id=str(a.id),
                    source=str(a.from_node_id),
                    target=str(a.to_node_id),
                    anchor_type=a.anchor_type,
                    weight=a.weight or 1,
                )
                for a in anchors
            ],
        )

    # ------------------------------------------------------------------
    # Type-safe factory
    # ------------------------------------------------------------------


def create_graph_repository(db_session: AsyncSession) -> AsyncGraphRepository:
    return AsyncGraphRepository(db_session)
