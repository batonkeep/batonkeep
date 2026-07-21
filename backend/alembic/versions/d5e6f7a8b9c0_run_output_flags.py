"""run output flags (task-lane output advisories)

One additive nullable JSON column, `runs.output_flags`, mirroring
`session_turns.output_flags` (P-0069): free-default unbacked-file-claim flags and
the `outputs_missing` sub-task-contract advisory, now on the task lane too. NULL =
clean. Advisory only — never changes the run's success/failure status.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-21 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd5e6f7a8b9c0'
down_revision: str | None = 'c4d5e6f7a8b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('output_flags', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'output_flags')
