"""code-exec execution policy (P-0046)

Adds Session.exec_policy and Task.exec_policy — the per-session/per-task code-exec
execution policy (off | confirmation | allow-safe | auto). Default confirmation.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-14 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows; the ORM-side default is client-side
    # (the drift guard does not compare server defaults).
    for table in ('sessions', 'tasks'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    'exec_policy', sa.String(length=16),
                    nullable=False, server_default='confirmation',
                )
            )


def downgrade() -> None:
    for table in ('sessions', 'tasks'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('exec_policy')
