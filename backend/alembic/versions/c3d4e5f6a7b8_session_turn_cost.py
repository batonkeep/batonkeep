"""session-turn token/cost usage

Adds SessionTurn.tokens_in / tokens_out / cost_usd so build-session (API-path)
spend is metered and shows in Analytics + counts toward the budget (previously
always $0 — the executor reported usage but the columns did not exist).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-14 05:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('session_turns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('session_turns', schema=None) as batch_op:
        batch_op.drop_column('cost_usd')
        batch_op.drop_column('tokens_out')
        batch_op.drop_column('tokens_in')
