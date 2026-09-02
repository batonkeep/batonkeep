"""P-0103 / D-0059 D3: attribution envelope on material events

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-02

Adds `principal_id` / `principal_kind` / `initiated_by` / `executed_by` / `delegated_by`
to `approvals` and `evidence` — the two material-event tables that exist today.

All nullable, and **existing rows are deliberately left NULL rather than backfilled**.
A guessed envelope is worse than an absent one: it would assert an actor nobody verified,
in exactly the record whose value is that it can be trusted. NULL reads honestly as
"written before attribution existed".
"""
from alembic import op
import sqlalchemy as sa

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None

_COLS = (
    ("principal_id", sa.String(length=128)),
    ("principal_kind", sa.String(length=16)),
    ("initiated_by", sa.String(length=128)),
    ("executed_by", sa.String(length=128)),
    ("delegated_by", sa.String(length=128)),
)


def upgrade() -> None:
    for table in ("approvals", "evidence"):
        for name, type_ in _COLS:
            op.add_column(table, sa.Column(name, type_, nullable=True))
        op.create_index(f"ix_{table}_principal_id", table, ["principal_id"])


def downgrade() -> None:
    for table in ("approvals", "evidence"):
        op.drop_index(f"ix_{table}_principal_id", table_name=table)
        for name, _ in _COLS:
            op.drop_column(table, name)
