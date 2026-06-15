"""session budget + prompt-cache token accounting

Adds:
  - sessions.budget_usd — optional per-session spend cap (USD, API path). NULL =
    no session cap (opt-in); enforced cumulatively across turns by the executor
    budget gate, composed with the owner daily cap (lower wins).
  - session_turns.cache_read_tokens / cache_write_tokens — the prompt-cache token
    split so cached input bills at the cache-read/write rate rather than full input
    rate. Both default 0 (no behaviour change until caching is on).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-15 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('budget_usd', sa.Float(), nullable=True))
    with op.batch_alter_table('session_turns', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'cache_read_tokens', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column(
            'cache_write_tokens', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('session_turns', schema=None) as batch_op:
        batch_op.drop_column('cache_write_tokens')
        batch_op.drop_column('cache_read_tokens')
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('budget_usd')
