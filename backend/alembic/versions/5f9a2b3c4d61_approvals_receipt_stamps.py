"""durable approvals table + context-receipt provenance stamps

Two substrate-hardening additions:

- `approvals` — durable human-approval rows. The in-process Future stays the
  wakeup mechanism for an awaiting coroutine; the row is the record: it
  survives restarts (stale pending rows are expired by the startup reaper) and
  is the audit trail for both the code-exec confirmation round-trip and the
  new canonical-context write proposals.
- `context_receipts.harness_version` / `.cli_version` — provenance stamps so
  provider-fit comparisons can distinguish model regressions from CLI/harness
  regressions.

Revision ID: 5f9a2b3c4d61
Revises: 4e8b1c9d2f35
Create Date: 2026-07-16 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '5f9a2b3c4d61'
down_revision: Union[str, None] = '4e8b1c9d2f35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'approvals',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=True),
        sa.Column('work_item_id', sa.Integer(), nullable=True),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('producer', sa.String(length=96), nullable=False),
        sa.Column('decided_by', sa.String(length=96), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['work_item_id'], ['work_items.id']),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id']),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id'),
    )
    op.create_index(op.f('ix_approvals_owner_id'), 'approvals', ['owner_id'])
    op.create_index(op.f('ix_approvals_status'), 'approvals', ['status'])
    op.create_index(op.f('ix_approvals_project_id'), 'approvals', ['project_id'])
    op.create_index(op.f('ix_approvals_work_item_id'), 'approvals', ['work_item_id'])
    op.create_index(op.f('ix_approvals_session_id'), 'approvals', ['session_id'])
    op.create_index(op.f('ix_approvals_run_id'), 'approvals', ['run_id'])

    with op.batch_alter_table('context_receipts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('harness_version', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('cli_version', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('context_receipts', schema=None) as batch_op:
        batch_op.drop_column('cli_version')
        batch_op.drop_column('harness_version')
    op.drop_index(op.f('ix_approvals_run_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_session_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_work_item_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_project_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_status'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_owner_id'), table_name='approvals')
    op.drop_table('approvals')
