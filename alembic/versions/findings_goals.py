"""Add findings and goals tables

Revision ID: findings_goals
Revises: initial
Create Date: 2026-10-14

Adds the Finding (vulnerability) and Goal tables. Enum types (severity,
finding_status, goal_status) are created by the sa.Enum(...) columns via
SQLAlchemy's PostgreSQL DDL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "findings_goals"
down_revision: Union[str, None] = "initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOTE: severity / finding_status / goal_status are created by the
    # sa.Enum(...) columns below. No separate CREATE TYPE needed.

    op.create_table(
        "findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", sa.Enum("critical", "high", "medium", "low", "info", name="severity"), nullable=False),
        sa.Column("status", sa.Enum("candidate", "confirmed", "false_positive", "fixed", "retesting", name="finding_status"), nullable=False),
        sa.Column("vuln_type", sa.String(length=100), nullable=True),
        sa.Column("affected_asset", sa.String(length=500), nullable=True),
        sa.Column("cvss_score", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("remediation", sa.Text(), nullable=True),
        sa.Column("source_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("extra_data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_findings_task_id"), "findings", ["task_id"], unique=False)
    op.create_index(op.f("ix_findings_severity"), "findings", ["severity"], unique=False)
    op.create_index(op.f("ix_findings_status"), "findings", ["status"], unique=False)

    op.create_table(
        "goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("pending", "in_progress", "met", "failed", name="goal_status"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("met_at", sa.DateTime(), nullable=True),
        sa.Column("extra_data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_goals_task_id"), "goals", ["task_id"], unique=False)
    op.create_index(op.f("ix_goals_status"), "goals", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_goals_status"), table_name="goals")
    op.drop_index(op.f("ix_goals_task_id"), table_name="goals")
    op.drop_table("goals")
    op.drop_index(op.f("ix_findings_status"), table_name="findings")
    op.drop_index(op.f("ix_findings_severity"), table_name="findings")
    op.drop_index(op.f("ix_findings_task_id"), table_name="findings")
    op.drop_table("findings")
    for t in ("goal_status", "finding_status", "severity"):
        op.execute(f"DROP TYPE {t} IF EXISTS")
