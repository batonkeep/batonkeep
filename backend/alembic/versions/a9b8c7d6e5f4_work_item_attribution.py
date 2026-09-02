"""D-0072: attribution envelope on work items (agent-to-agent hand-off)

Revision ID: a9b8c7d6e5f4
Revises: e9f0a1b2c3d4
Create Date: 2026-09-02

[[D-0059]] D3 put the envelope on `approvals` and `evidence` — the material-event
tables that existed when [[P-0103]] landed. `work_items` now joins them, because
[[D-0072]] makes a work item the **one thing that crosses between agents**: agent A
asks agent B by minting a `proposed` item in B's project. From that point on, "which
agent proposed this, from where" is a question the row must be able to answer on its
own — the sending planner run lives in a different project and may be long gone.

Same shape, same nullability, and the same refusal to backfill: an item minted before
this migration was minted by *something*, but nobody recorded what, and guessing would
put an unverified actor into the record whose whole value is that it can be trusted.
NULL reads honestly as "written before attribution existed".
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b8c7d6e5f4"
down_revision = "e9f0a1b2c3d4"
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
    for name, type_ in _COLS:
        op.add_column("work_items", sa.Column(name, type_, nullable=True))
    op.create_index("ix_work_items_principal_id", "work_items", ["principal_id"])


def downgrade() -> None:
    op.drop_index("ix_work_items_principal_id", table_name="work_items")
    for name, _ in _COLS:
        op.drop_column("work_items", name)
