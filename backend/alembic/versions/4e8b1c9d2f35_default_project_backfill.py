"""default-project backfill: every owner gets a "Personal workspace" Project

Data-only companion to 9d3f2c8b7a10: creates the per-owner default Project and
attaches every existing task, session, and run to it, so a v0.6.0 database
upgrades with zero loss and zero behavior change. Idempotent — owners that
already have a default Project are skipped, and only NULL project_id rows are
backfilled (a re-run or a partially-migrated DB converges to the same state).

Revision ID: 4e8b1c9d2f35
Revises: 9d3f2c8b7a10
Create Date: 2026-07-15 00:00:01.000000
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '4e8b1c9d2f35'
down_revision: Union[str, None] = '9d3f2c8b7a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_PROJECT_NAME = "Personal workspace"


def upgrade() -> None:
    bind = op.get_bind()

    owners = bind.execute(sa.text("SELECT id FROM owners")).scalars().all()
    for owner_id in owners:
        default_id = bind.execute(
            sa.text(
                "SELECT id FROM projects WHERE owner_id = :o AND is_default = 1 LIMIT 1"
            ),
            {"o": owner_id},
        ).scalar()
        if default_id is None:
            default_id = uuid.uuid4().hex
            bind.execute(
                sa.text(
                    "INSERT INTO projects (id, owner_id, name, kind, status, sensitivity,"
                    " is_default) VALUES (:id, :o, :name, 'general', 'active', 'normal', 1)"
                ),
                {"id": default_id, "o": owner_id, "name": DEFAULT_PROJECT_NAME},
            )

        for table in ("tasks", "sessions"):
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET project_id = :p"
                    f" WHERE owner_id = :o AND project_id IS NULL"
                ),
                {"p": default_id, "o": owner_id},
            )
        # Runs inherit their task's project (covers cross-owner edge cases exactly).
        bind.execute(
            sa.text(
                "UPDATE runs SET project_id ="
                " (SELECT project_id FROM tasks WHERE tasks.id = runs.task_id)"
                " WHERE owner_id = :o AND project_id IS NULL"
            ),
            {"o": owner_id},
        )


def downgrade() -> None:
    # Data-only revision: schema downgrade is 9d3f2c8b7a10's job. Detaching rows
    # from the default Project would discard information; deliberately a no-op.
    pass
