"""batch canonical approval (P-0077): approvals.batch_id

One additive nullable column. A batch decision stamps the same id onto every
row it settles, which is what makes "one decision over a related set"
reconstructable afterwards — the set is a property of the *decision*, not of
the proposals, so it cannot live in the proposal payload. NULL for every
individually-decided row, including all existing history.

Revision ID: 3f1347e9a746
Revises: e6f7a8b9c0d1
Create Date: 2026-07-21 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '3f1347e9a746'
down_revision: str | None = 'e6f7a8b9c0d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('approvals', sa.Column('batch_id', sa.String(length=32), nullable=True))
    op.create_index(op.f('ix_approvals_batch_id'), 'approvals', ['batch_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_approvals_batch_id'), table_name='approvals')
    op.drop_column('approvals', 'batch_id')
