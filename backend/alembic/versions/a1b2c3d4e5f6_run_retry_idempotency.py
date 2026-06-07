"""run retry + idempotency (P-0025 #2)

Adds Run.retry_count (bounded in-process retry counter) and Run.idempotency_key
(enqueue dedupe), plus the index for the key.

Revision ID: a1b2c3d4e5f6
Revises: 568318e3701c
Create Date: 2026-06-07 22:10:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '568318e3701c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        # server_default backfills existing rows; the ORM-side default is client-side
        # (the drift guard does not compare server defaults).
        batch_op.add_column(
            sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0')
        )
        batch_op.add_column(
            sa.Column('idempotency_key', sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            batch_op.f('ix_runs_idempotency_key'), ['idempotency_key'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_idempotency_key'))
        batch_op.drop_column('idempotency_key')
        batch_op.drop_column('retry_count')
