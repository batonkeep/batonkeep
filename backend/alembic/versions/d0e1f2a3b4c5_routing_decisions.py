"""routing-signal backbone: routing_decisions table (P-0053)

Adds the `routing_decisions` table — a structured record of each routing decision
(what the router considered + why, captured at decision time by router.RoutingTrace).
Content-free; owner-scoped (P3); `run_id` nullable so session-level routing can reuse
it later. The spine for the deferred smart-routing policy.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-16 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd0e1f2a3b4c5'
down_revision: str | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'routing_decisions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False, server_default='local'),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('task_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column('policy_version', sa.String(length=32), nullable=False, server_default='rule-v1'),
        sa.Column('strategy', sa.String(length=32), nullable=False),
        sa.Column('confidential', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('degraded', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('deployment_mode', sa.String(length=16), nullable=True),
        sa.Column('deferred', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('deciding_reason', sa.String(length=256), nullable=True),
        sa.Column('requested_candidates', sa.JSON(), nullable=True),
        sa.Column('evaluated', sa.JSON(), nullable=True),
        sa.Column('chosen', sa.String(length=96), nullable=True),
        sa.Column('chosen_candidates', sa.JSON(), nullable=True),
        sa.Column('overflow_to', sa.String(length=96), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_routing_decisions_owner_id', 'routing_decisions', ['owner_id'])
    op.create_index('ix_routing_decisions_run_id', 'routing_decisions', ['run_id'])
    op.create_index('ix_routing_decisions_task_id', 'routing_decisions', ['task_id'])


def downgrade() -> None:
    op.drop_index('ix_routing_decisions_task_id', table_name='routing_decisions')
    op.drop_index('ix_routing_decisions_run_id', table_name='routing_decisions')
    op.drop_index('ix_routing_decisions_owner_id', table_name='routing_decisions')
    op.drop_table('routing_decisions')
