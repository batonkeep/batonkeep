"""routing-decision outcome linkage (P-0053 slice 2)

Adds the outcome columns filled at run finalization, closing the
(decision → realized outcome) tuple: terminal status, the provider/model that
actually executed, whether failover was used, attempt count, and realized
cost/latency. All nullable (a decision may be deferred/cancelled before any
outcome, and pre-slice-2 rows stay NULL).

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-06-17 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e1f2a3b4c5d6'
down_revision: str | None = 'd0e1f2a3b4c5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('routing_decisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('outcome_status', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('executed_provider', sa.String(length=96), nullable=True))
        batch_op.add_column(sa.Column('executed_model', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('failover_used', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('attempt_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('outcome_cost_usd', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('outcome_duration_ms', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('outcome_at', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('routing_decisions', schema=None) as batch_op:
        batch_op.drop_column('outcome_at')
        batch_op.drop_column('outcome_duration_ms')
        batch_op.drop_column('outcome_cost_usd')
        batch_op.drop_column('attempt_count')
        batch_op.drop_column('failover_used')
        batch_op.drop_column('executed_model')
        batch_op.drop_column('executed_provider')
        batch_op.drop_column('outcome_status')
