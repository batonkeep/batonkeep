"""P-0106: approval checkpoint for parked runs

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-01

Adds `approvals.checkpoint` — the parked run's provider-native conversation, so an
approval decided after a restart can be acted on instead of the run having died with the
process. Nullable: every existing row predates parking and has nothing to resume.

No column is needed for the new `runs.status = 'parked'` value; status is a free-form
String(16) ("parked" is 6).
"""
from alembic import op
import sqlalchemy as sa

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("checkpoint", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "checkpoint")
