"""D-0080: an explicit `lane` on approvals

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-09-15

`kind` was doing two jobs. It named the **act** (`canonical_write`, `schedule_proposal`)
and, at three call sites, stood in for **what kind of thing the approval gates** — "is
this an unattended run's tool request" was spelled `kind == "code_exec"`. That held while
code execution was the only gated tool and stopped holding when `browser_open` rode the
same lane ([[D-0079]]).

**Backfilled, unlike the attribution envelope, and the difference matters.** The envelope
was left NULL because the actor of an old row is genuinely unknown and guessing would put
an unverified claim into an audit record. Here the mapping is **total and determined**:
every existing row's lane follows from its kind with no inference —
`canonical_write`/`schedule_proposal` are proposals, everything else gates a tool. There
is no judgement to get wrong, so leaving rows NULL would be false modesty that every
reader then has to work around.

**It also fixes a live defect.** `reap_pending` expires stranded rows on restart and
excluded exactly one kind by name (`kind != "canonical_write"`), while its docstring gave
the *property*: "carry no Future and stay decidable across restarts". `schedule_proposal`
has that property and was not named, so **every pending schedule proposal an agent made
([[D-0070]]) was expired at the next restart**, before the operator could see it. Keying
the sweep on `lane` states the rule the docstring already stated.
"""
import sqlalchemy as sa

from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None

#: Kinds that are durable proposals rather than gates on a live execution.
_PROPOSAL_KINDS = ("canonical_write", "schedule_proposal")


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("lane", sa.String(length=16), nullable=False, server_default="tool"),
    )
    op.create_index("ix_approvals_lane", "approvals", ["lane"])
    conn = op.get_bind()
    result = conn.execute(
        sa.text("UPDATE approvals SET lane = 'proposal' WHERE kind IN :kinds").bindparams(
            sa.bindparam("kinds", value=_PROPOSAL_KINDS, expanding=True)
        )
    )
    print(f"[D-0080] {result.rowcount} approval(s) classified as the proposal lane")


def downgrade() -> None:
    op.drop_index("ix_approvals_lane", table_name="approvals")
    op.drop_column("approvals", "lane")
