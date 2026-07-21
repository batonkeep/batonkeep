"""planner agent (P-0078): per-project planner selection + planner_runs audit

Two additive Project columns (`planner_provider`, `planner_model`) holding the
per-project planner default for meta-work — NULL falls back to the global executor
default, so a planner is never a mandatory per-project decision. Plus the
`planner_runs` table: the audit + spend trail for planning-turn invocations (the
proposer-only meta-work lane). No workspace — planning is DB-state meta-work.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-21 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e6f7a8b9c0d1'
down_revision: str | None = 'd5e6f7a8b9c0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('planner_provider', sa.String(length=96), nullable=True))
    op.add_column('projects', sa.Column('planner_model', sa.String(length=128), nullable=True))
    op.create_table(
        'planner_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('work_item_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('provider', sa.String(length=96), nullable=True),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('local_pinned', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('request', sa.Text(), nullable=True),
        sa.Column('response', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('proposals', sa.JSON(), nullable=True),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['work_item_id'], ['work_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_planner_runs_owner_id', 'planner_runs', ['owner_id'])
    op.create_index('ix_planner_runs_project_id', 'planner_runs', ['project_id'])
    op.create_index('ix_planner_runs_work_item_id', 'planner_runs', ['work_item_id'])
    op.create_index('ix_planner_runs_status', 'planner_runs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_planner_runs_status', table_name='planner_runs')
    op.drop_index('ix_planner_runs_work_item_id', table_name='planner_runs')
    op.drop_index('ix_planner_runs_project_id', table_name='planner_runs')
    op.drop_index('ix_planner_runs_owner_id', table_name='planner_runs')
    op.drop_table('planner_runs')
    op.drop_column('projects', 'planner_model')
    op.drop_column('projects', 'planner_provider')
