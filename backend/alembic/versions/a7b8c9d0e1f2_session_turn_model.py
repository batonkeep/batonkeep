"""session_turn model id (P-0049 usage metrics)

Adds:
  - session_turns.model — the effective model id a build-session turn ran, so
    per-model usage metrics (the catalog picker's most/recently-used sort) count
    build sessions, not just task Runs. NULL for pre-existing turns.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-15 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('session_turns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model', sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session_turns', schema=None) as batch_op:
        batch_op.drop_column('model')
