"""Initial migration: create dual-graph schema

Revision ID: initial
Revises:
Create Date: 2026-10-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOTE: enum types (asset_type, risk_level, ...) are created by the
    # sa.Enum(...) columns below via SQLAlchemy's PostgreSQL DDL.
    # Do NOT also run a manual CREATE TYPE here (it caused "already exists").

    # Companies
    op.create_table('companies',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=True),
        sa.Column('industry', sa.String(length=100), nullable=True),
        sa.Column('contact_email', sa.String(length=255), nullable=True),
        sa.Column('contact_phone', sa.String(length=50), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(), nullable=True),
        sa.Column('is_active', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_companies_domain'), 'companies', ['domain'], unique=True)
    op.create_index(op.f('ix_companies_name'), 'companies', ['name'], unique=False)
    op.create_index(op.f('ix_companies_is_active'), 'companies', ['is_active'], unique=False)

    # Assets
    op.create_table('assets',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('company_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('asset_type', sa.Enum('domain', 'ip', 'service', 'endpoint', name='asset_type'), nullable=False),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('domain', sa.String(length=255), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('service_name', sa.String(length=100), nullable=True),
        sa.Column('protocol', sa.String(length=20), nullable=True),
        sa.Column('os_guess', sa.String(length=100), nullable=True),
        sa.Column('risk_level', sa.Enum('critical', 'high', 'medium', 'low', 'info', name='risk_level'), nullable=True),
        sa.Column('tags', postgresql.JSONB(), nullable=True),
        sa.Column('extra_data', postgresql.JSONB(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'ip_address', name='uq_asset_company_ip'),
        sa.UniqueConstraint('company_id', 'domain', name='uq_asset_company_domain')
    )
    op.create_index(op.f('ix_assets_company_id'), 'assets', ['company_id'], unique=False)
    op.create_index(op.f('ix_assets_asset_type'), 'assets', ['asset_type'], unique=False)
    op.create_index(op.f('ix_assets_ip_address'), 'assets', ['ip_address'], unique=False)
    op.create_index(op.f('ix_assets_domain'), 'assets', ['domain'], unique=False)
    op.create_index(op.f('ix_assets_is_active'), 'assets', ['is_active'], unique=False)

    # Tasks
    op.create_table('tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('company_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('asset_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('task_type', sa.Enum('recon', 'scan', 'exploit', 'post_exploit', 'full', name='task_type'), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('pending', 'running', 'paused', 'completed', 'failed', 'cancelled', name='task_status'), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('max_iterations', sa.Integer(), nullable=True),
        sa.Column('current_iteration', sa.Integer(), nullable=False),
        sa.Column('agent_config', postgresql.JSONB(), nullable=True),
        sa.Column('context_data', postgresql.JSONB(), nullable=True),
        sa.Column('result_summary', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('planner_state_json', postgresql.JSONB(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tasks_company_id'), 'tasks', ['company_id'], unique=False)
    op.create_index(op.f('ix_tasks_asset_id'), 'tasks', ['asset_id'], unique=False)
    op.create_index(op.f('ix_tasks_task_type'), 'tasks', ['task_type'], unique=False)
    op.create_index(op.f('ix_tasks_status'), 'tasks', ['status'], unique=False)

    # Exploration Nodes
    op.create_table('exploration_nodes',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('node_type', sa.Enum('goal', 'intent', 'fact', 'finding', 'clue', 'tool_output', 'hypothesis', 'action', name='exploration_node_type'), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=100), nullable=True),
        sa.Column('tool_name', sa.String(length=100), nullable=True),
        sa.Column('extra_data', postgresql.JSONB(), nullable=True),
        sa.Column('is_verified', sa.Integer(), nullable=False),
        sa.Column('depth_level', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_exploration_nodes_task_id'), 'exploration_nodes', ['task_id'], unique=False)
    op.create_index(op.f('ix_exploration_nodes_node_type'), 'exploration_nodes', ['node_type'], unique=False)

    # Exploration Anchors
    op.create_table('exploration_anchors',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('from_node_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('to_node_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('anchor_type', sa.Enum('supports', 'contradicts', 'leads_to', 'related_to', 'question_for', name='anchor_type'), nullable=False),
        sa.Column('weight', sa.Integer(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['from_node_id'], ['exploration_nodes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['to_node_id'], ['exploration_nodes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('from_node_id', 'to_node_id', name='uq_anchor_pair')
    )
    op.create_index(op.f('ix_exploration_anchors_from_node_id'), 'exploration_anchors', ['from_node_id'], unique=False)
    op.create_index(op.f('ix_exploration_anchors_to_node_id'), 'exploration_anchors', ['to_node_id'], unique=False)

    # Activities
    op.create_table('activities',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('intent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('activity_type', sa.Enum('tool_call', 'tool_result', 'agent_thought', 'state_update', 'milestone', 'error', 'human_intervention', name='activity_type'), nullable=False),
        sa.Column('step_number', sa.Integer(), nullable=False),
        sa.Column('sequence_id', sa.String(length=50), nullable=True),
        sa.Column('tool_name', sa.String(length=100), nullable=True),
        sa.Column('tool_input', postgresql.JSONB(), nullable=True),
        sa.Column('tool_output', postgresql.JSONB(), nullable=True),
        sa.Column('agent_thought', sa.Text(), nullable=True),
        sa.Column('result_data', postgresql.JSONB(), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['intent_id'], ['exploration_nodes.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_activities_task_id'), 'activities', ['task_id'], unique=False)
    op.create_index(op.f('ix_activities_intent_id'), 'activities', ['intent_id'], unique=False)
    op.create_index(op.f('ix_activities_activity_type'), 'activities', ['activity_type'], unique=False)

    # Intents (distributed atomic claim table)
    op.create_table('intents',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('intent_type', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('prerequisite_node_ids', postgresql.JSONB(), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'claimed', 'completed', 'failed', 'expired', name='intent_status'), nullable=False),
        sa.Column('claimed_by_worker_id', sa.String(length=100), nullable=True),
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('result_node_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('extra_data', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_intents_task_id'), 'intents', ['task_id'], unique=False)
    op.create_index(op.f('ix_intents_status'), 'intents', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_intents_status'), table_name='intents')
    op.drop_index(op.f('ix_intents_task_id'), table_name='intents')
    op.drop_table('intents')
    op.drop_index(op.f('ix_activities_activity_type'), table_name='activities')
    op.drop_index(op.f('ix_activities_intent_id'), table_name='activities')
    op.drop_index(op.f('ix_activities_task_id'), table_name='activities')
    op.drop_table('activities')
    op.drop_index(op.f('ix_exploration_anchors_to_node_id'), table_name='exploration_anchors')
    op.drop_index(op.f('ix_exploration_anchors_from_node_id'), table_name='exploration_anchors')
    op.drop_table('exploration_anchors')
    op.drop_index(op.f('ix_exploration_nodes_node_type'), table_name='exploration_nodes')
    op.drop_index(op.f('ix_exploration_nodes_task_id'), table_name='exploration_nodes')
    op.drop_table('exploration_nodes')
    op.drop_index(op.f('ix_tasks_status'), table_name='tasks')
    op.drop_index(op.f('ix_tasks_task_type'), table_name='tasks')
    op.drop_index(op.f('ix_tasks_asset_id'), table_name='tasks')
    op.drop_index(op.f('ix_tasks_company_id'), table_name='tasks')
    op.drop_table('tasks')
    op.drop_index(op.f('ix_assets_is_active'), table_name='assets')
    op.drop_index(op.f('ix_assets_domain'), table_name='assets')
    op.drop_index(op.f('ix_assets_ip_address'), table_name='assets')
    op.drop_index(op.f('ix_assets_asset_type'), table_name='assets')
    op.drop_index(op.f('ix_assets_company_id'), table_name='assets')
    op.drop_table('assets')
    op.drop_index(op.f('ix_companies_is_active'), table_name='companies')
    op.drop_index(op.f('ix_companies_name'), table_name='companies')
    op.drop_index(op.f('ix_companies_domain'), table_name='companies')
    op.drop_table('companies')
    # Drop enum types after all tables referencing them are gone.
    for t in ('activity_type', 'anchor_type', 'exploration_node_type',
              'task_status', 'task_type', 'risk_level', 'intent_status', 'asset_type'):
        op.execute(f"DROP TYPE {t} IF EXISTS")
