"""work-item evidence pins + receipt evidence payload

Two additive nullable JSON columns closing the knowledge loop:

- `work_items.pinned_evidence` — operator-curated inputs for a work item
  ({"v":1,"items":[{"evidence_id","note"?}]}); the projection materializes
  them read-only into the workspace's `context/evidence/`. Pins are work-item
  state so the Evidence table keeps its append-only no-update contract.
- `context_receipts.evidence` — what evidence an actor actually received
  ({"v":1,"index_count","index_sha","materialized":[…],"exclusions":[…]}),
  extending the receipt's paths-and-hashes discipline to evidence.

Revision ID: 6a0b3c4d5e72
Revises: 5f9a2b3c4d61
Create Date: 2026-07-19 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '6a0b3c4d5e72'
down_revision: str | None = '5f9a2b3c4d61'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('work_items', sa.Column('pinned_evidence', sa.JSON(), nullable=True))
    op.add_column('context_receipts', sa.Column('evidence', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('context_receipts', 'evidence')
    op.drop_column('work_items', 'pinned_evidence')
