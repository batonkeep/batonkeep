"""per-task run timeout (P-0056/D-0052)

Adds Task.timeout_seconds — an optional per-task run-timeout override in seconds.
NULL = inherit the global run_timeout_seconds default (1800s). Bounds elapsed
wall-clock time for the whole run; the API clamps it to a 6h ceiling.

Revision ID: a2b3c4d5e6f7
Revises: e1f2a3b4c5d6
Create Date: 2026-06-19 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('timeout_seconds', sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('timeout_seconds')
