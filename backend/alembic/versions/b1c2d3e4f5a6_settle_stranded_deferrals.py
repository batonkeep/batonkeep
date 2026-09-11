"""P-0112/D-0076: settle runs deferred with no resolution time

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-09-11

The code change makes a null `deferred_until` unwritable. It does nothing for rows
already on disk, and those are the whole reason the defect was found: the always-on
testbed had **47** of them, accumulated over ten days, every one invisible to a sweep
that selects `deferred_until <= now`. The `before_flush` guard only fires on write, so
without this migration they would sit there until something happened to touch them —
which, being unschedulable, nothing ever would.

**What they become and why.** `failed`, matching [[D-0076]]'s policy exactly: a
deferral that cannot resolve on its own is not a deferral, and the failover path
reached that conclusion independently before the routing path did. The alternative —
leaving them `deferred` — keeps a status that promises a retry the system has now
formally stopped promising.

**They are marked, not merely changed.** The error text is distinctive on purpose:
an operator who finds a pile of freshly-failed runs must be able to tell that a
migration did this and that the runs were already dead, rather than concluding their
instance broke overnight. It is also what makes the downgrade honest — it can return
exactly the rows this touched and no others.

**Narrow by construction:** `status = 'deferred' AND deferred_until IS NULL`. A
deferral with a time is a working deferral and is left alone; nothing else is in scope.
`finished_at` is only set where it is absent, so an existing timestamp is never
overwritten.
"""
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None

# The marker. Load-bearing in both directions: it tells an operator what happened,
# and it is how `downgrade` identifies precisely the rows `upgrade` changed.
_ERROR = (
    "deferred with no resolution time — settled as failed by the P-0112 fix. "
    "This run was never schedulable: the deferred-run sweep selects on "
    "deferred_until, and this row had none, so it could not have resumed. "
    "Requeue the task if the work is still wanted."
)


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            """
            UPDATE runs
               SET status = 'failed',
                   error = :err,
                   finished_at = COALESCE(finished_at, :now)
             WHERE status = 'deferred'
               AND deferred_until IS NULL
            """
        ),
        {"err": _ERROR, "now": datetime.now(UTC)},
    )
    # Visible in the migration log — on a healthy instance this is 0, and an operator
    # who sees a large number has just been told something true about their deployment.
    print(f"[P-0112] settled {result.rowcount} stranded deferral(s)")


def downgrade() -> None:
    """Return exactly the rows `upgrade` settled — identified by the marker, so a
    run that failed for any other reason is never resurrected into a state the
    current code refuses to write."""
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE runs
               SET status = 'deferred',
                   error = NULL,
                   finished_at = NULL
             WHERE status = 'failed'
               AND error = :err
            """
        ),
        {"err": _ERROR},
    )
