"""task-run generated-asset handling (P-0050/D-0046)

Adds the `run_assets` child table (a non-md/json artifact a task run produced —
generated image, agent-written csv/pdf) and per-task retention caps on `tasks`
(`asset_max_count` / `asset_max_bytes`; NULL = unlimited).

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-16 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'run_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('rel_path', sa.String(length=512), nullable=False),
        sa.Column('mime', sa.String(length=128), nullable=True),
        sa.Column('bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_run_assets_run_id', 'run_assets', ['run_id'])

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('asset_max_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('asset_max_bytes', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('asset_max_bytes')
        batch_op.drop_column('asset_max_count')

    op.drop_index('ix_run_assets_run_id', table_name='run_assets')
    op.drop_table('run_assets')
