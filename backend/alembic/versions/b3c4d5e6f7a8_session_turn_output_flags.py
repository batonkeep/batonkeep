"""session-turn output flags (free default file-claim check)

One additive nullable JSON column, `session_turns.output_flags`, storing the
result of the post-turn free default check (P-0069 item 6): file paths a turn's
response links to that are NOT backed by this session's committed tree — either
this session's raw-file links whose rel path isn't tracked, or a leftover
`file://` link pointing at an absolute/foreign-workspace path (the P43-D3 tell of
work that landed in another session's worktree). Shape:
{"v":1,"unbacked":[<path>,…]}. NULL = the check found nothing (or predates it).

Revision ID: b3c4d5e6f7a8
Revises: 6a0b3c4d5e72
Create Date: 2026-07-21 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b3c4d5e6f7a8'
down_revision: str | None = '6a0b3c4d5e72'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('session_turns', sa.Column('output_flags', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('session_turns', 'output_flags')
