"""work-item sub-task checklist (output contract + grounded progress)

One additive nullable JSON column, `work_items.subtasks`, holding the lightweight
sub-task checklist (P-0069 item 6, B2): {"v":1,"items":[{id,label,expected?,status,
done,verified,verified_at,proposed_by}]}. A verifiable item (declares `expected`)
is done+verified only when its glob matches a file in the committed tree; asserted
items (no artifact) can be done but stay unverified. NULL = no checklist. Items are
agent-proposed and operator-confirmed/modified ([[P-0078]] planner).

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-21 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c4d5e6f7a8b9'
down_revision: str | None = 'b3c4d5e6f7a8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('work_items', sa.Column('subtasks', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('work_items', 'subtasks')
