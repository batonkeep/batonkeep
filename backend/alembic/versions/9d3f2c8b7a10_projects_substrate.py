"""projects substrate: Project/WorkItem/ContextSource/ContextReceipt/Evidence (S0)

Creates the durable-work substrate tables and threads nullable project/work-item
columns onto tasks, runs, sessions, and routing_decisions. Columns stay nullable
at the DB level during the staged migration (the API resolves and writes the
owner's default Project); the companion revision 4e8b1c9d2f35 backfills existing
rows into a per-owner default "Personal workspace" Project.

Revision ID: 9d3f2c8b7a10
Revises: a2b3c4d5e6f7
Create Date: 2026-07-15 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9d3f2c8b7a10'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False, server_default='local'),
        sa.Column('name', sa.String(length=256), nullable=False,
                  server_default='Untitled project'),
        sa.Column('kind', sa.String(length=64), nullable=False, server_default='general'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('sensitivity', sa.String(length=16), nullable=False, server_default='normal'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('root_path', sa.String(length=512), nullable=True),
        sa.Column('manifest_rel', sa.String(length=256), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projects_owner_id', 'projects', ['owner_id'])

    op.create_table(
        'work_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False, server_default='local'),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=64), nullable=False, server_default='task'),
        sa.Column('state', sa.String(length=24), nullable=False, server_default='open'),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('objective', sa.Text(), nullable=False, server_default=''),
        sa.Column('next_action', sa.Text(), nullable=True),
        sa.Column('risk', sa.String(length=16), nullable=False, server_default='low'),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('signal', sa.JSON(), nullable=True),
        sa.Column('decisions', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['parent_id'], ['work_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_work_items_owner_id', 'work_items', ['owner_id'])
    op.create_index('ix_work_items_project_id', 'work_items', ['project_id'])
    op.create_index('ix_work_items_state', 'work_items', ['state'])

    op.create_table(
        'context_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False, server_default='local'),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='file'),
        sa.Column('rel_path', sa.String(length=512), nullable=False),
        sa.Column('bootstrap_order', sa.Integer(), nullable=True),
        sa.Column('domain', sa.String(length=64), nullable=True),
        sa.Column('sensitivity', sa.String(length=16), nullable=False, server_default='inherit'),
        sa.Column('last_revision', sa.String(length=64), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_context_sources_owner_id', 'context_sources', ['owner_id'])
    op.create_index('ix_context_sources_project_id', 'context_sources', ['project_id'])

    op.create_table(
        'context_receipts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False, server_default='local'),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('work_item_id', sa.Integer(), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('session_turn_id', sa.Integer(), nullable=True),
        sa.Column('projection_version', sa.String(length=16), nullable=False,
                  server_default='proj-v1'),
        sa.Column('sources', sa.JSON(), nullable=True),
        sa.Column('ledger_sha', sa.String(length=64), nullable=True),
        sa.Column('exclusions', sa.JSON(), nullable=True),
        sa.Column('approx_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['work_item_id'], ['work_items.id']),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id']),
        sa.ForeignKeyConstraint(['session_turn_id'], ['session_turns.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_context_receipts_owner_id', 'context_receipts', ['owner_id'])
    op.create_index('ix_context_receipts_project_id', 'context_receipts', ['project_id'])
    op.create_index('ix_context_receipts_work_item_id', 'context_receipts', ['work_item_id'])
    op.create_index('ix_context_receipts_run_id', 'context_receipts', ['run_id'])
    op.create_index('ix_context_receipts_session_turn_id', 'context_receipts',
                    ['session_turn_id'])

    op.create_table(
        'evidence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=False, server_default='local'),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('work_item_id', sa.Integer(), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('session_turn_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False, server_default='report'),
        sa.Column('rel_path', sa.String(length=512), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=True),
        sa.Column('producer', sa.String(length=96), nullable=False, server_default='system'),
        sa.Column('bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sensitivity', sa.String(length=16), nullable=False, server_default='inherit'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['work_item_id'], ['work_items.id']),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id']),
        sa.ForeignKeyConstraint(['session_turn_id'], ['session_turns.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_evidence_owner_id', 'evidence', ['owner_id'])
    op.create_index('ix_evidence_project_id', 'evidence', ['project_id'])
    op.create_index('ix_evidence_work_item_id', 'evidence', ['work_item_id'])
    op.create_index('ix_evidence_run_id', 'evidence', ['run_id'])
    op.create_index('ix_evidence_session_turn_id', 'evidence', ['session_turn_id'])

    # ── Thread the substrate onto the existing execution tables ────────────────
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('work_item_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_tasks_project_id_projects', 'projects',
                                    ['project_id'], ['id'])
        batch_op.create_foreign_key('fk_tasks_work_item_id_work_items', 'work_items',
                                    ['work_item_id'], ['id'])
        batch_op.create_index('ix_tasks_project_id', ['project_id'])

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('work_item_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_runs_project_id_projects', 'projects',
                                    ['project_id'], ['id'])
        batch_op.create_foreign_key('fk_runs_work_item_id_work_items', 'work_items',
                                    ['work_item_id'], ['id'])
        batch_op.create_index('ix_runs_project_id', ['project_id'])
        batch_op.create_index('ix_runs_work_item_id', ['work_item_id'])

    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('work_item_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_sessions_project_id_projects', 'projects',
                                    ['project_id'], ['id'])
        batch_op.create_foreign_key('fk_sessions_work_item_id_work_items', 'work_items',
                                    ['work_item_id'], ['id'])
        batch_op.create_index('ix_sessions_project_id', ['project_id'])

    # Plain columns (no FK) — telemetry rows must never block on a deleted project.
    with op.batch_alter_table('routing_decisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('work_item_kind', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_routing_decisions_project_id', ['project_id'])


def downgrade() -> None:
    with op.batch_alter_table('routing_decisions', schema=None) as batch_op:
        batch_op.drop_index('ix_routing_decisions_project_id')
        batch_op.drop_column('work_item_kind')
        batch_op.drop_column('project_id')

    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_index('ix_sessions_project_id')
        batch_op.drop_constraint('fk_sessions_work_item_id_work_items', type_='foreignkey')
        batch_op.drop_constraint('fk_sessions_project_id_projects', type_='foreignkey')
        batch_op.drop_column('work_item_id')
        batch_op.drop_column('project_id')

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index('ix_runs_work_item_id')
        batch_op.drop_index('ix_runs_project_id')
        batch_op.drop_constraint('fk_runs_work_item_id_work_items', type_='foreignkey')
        batch_op.drop_constraint('fk_runs_project_id_projects', type_='foreignkey')
        batch_op.drop_column('work_item_id')
        batch_op.drop_column('project_id')

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index('ix_tasks_project_id')
        batch_op.drop_constraint('fk_tasks_work_item_id_work_items', type_='foreignkey')
        batch_op.drop_constraint('fk_tasks_project_id_projects', type_='foreignkey')
        batch_op.drop_column('work_item_id')
        batch_op.drop_column('project_id')

    _indexes = {
        'evidence': ['session_turn_id', 'run_id', 'work_item_id', 'project_id', 'owner_id'],
        'context_receipts': ['session_turn_id', 'run_id', 'work_item_id', 'project_id',
                             'owner_id'],
        'context_sources': ['project_id', 'owner_id'],
        'work_items': ['state', 'project_id', 'owner_id'],
        'projects': ['owner_id'],
    }
    for table, cols in _indexes.items():
        for col in cols:
            op.drop_index(f'ix_{table}_{col}', table_name=table)
        op.drop_table(table)
