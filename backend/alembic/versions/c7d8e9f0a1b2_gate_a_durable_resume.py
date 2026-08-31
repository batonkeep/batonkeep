"""Gate A: runtime_epoch on runs, recovery_policy on tasks (D-0067/D-0068)

Revision ID: c7d8e9f0a1b2
Revises: 3f1347e9a746
Create Date: 2026-08-31

Two columns behind boot-time reconciliation:

* ``runs.runtime_epoch`` — which backend process claimed the run, so startup can tell an
  orphan from a run a live sibling is driving instead of assuming that anything
  non-terminal at boot must be abandoned. Nullable: rows written before this column
  existed have no owner recorded and are treated as orphaned, which is the safe reading.

* ``tasks.recovery_policy`` — the *policy* half of resume. Server default
  ``next_occurrence`` so existing tasks adopt the conservative behaviour (do not re-run a
  stranded scheduled run; the next fire is the recovery), since double-firing is the worse
  failure. Backfilled explicitly rather than relying on the server default alone, so the
  column is never NULL for a reader that predates the default.
"""
from alembic import op
import sqlalchemy as sa

revision = "c7d8e9f0a1b2"
down_revision = "3f1347e9a746"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("runtime_epoch", sa.String(length=32), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "recovery_policy",
            sa.String(length=16),
            nullable=False,
            server_default="next_occurrence",
        ),
    )
    op.execute("UPDATE tasks SET recovery_policy = 'next_occurrence' WHERE recovery_policy IS NULL")


def downgrade() -> None:
    op.drop_column("tasks", "recovery_policy")
    op.drop_column("runs", "runtime_epoch")
