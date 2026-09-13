"""D-0073 D1b: per-task browser policy

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-13

`browser_policy` was a deployment-wide setting under D1a, and the code that read it said
why: *"instance-level for this slice (there is nothing to vary per task while the browser
is read-only and carries no credentials). The per-task policy arrives with D1b, where
interaction makes the distinction matter."* This is that.

**Nullable, with no backfill, and that is the whole design.** NULL means "use the
deployment default", which is exactly how every existing task behaves today. Stamping the
current `BROWSER_POLICY` onto every row would convert one setting an operator can change
into hundreds of frozen copies that no longer track it — the row would remember what the
default was on the day of the upgrade, forever. Same reasoning as [[D-0059]]'s refusal to
backfill an envelope: a value nobody chose should not be written as though they had.
"""
import sqlalchemy as sa

from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("browser_policy", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "browser_policy")
