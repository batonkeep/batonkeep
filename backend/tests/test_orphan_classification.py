"""Gate A / D-0068 — the boot-time resume decision, tested as a decision table.

`classify_orphan` is deliberately pure so the rule can be exercised without fixtures. The
rule it encodes has two layers, and the ordering between them is the whole design:

    safety (from evidence)  →  then, only if safe, policy (from intent)

A tag records what a job *wanted*; whether resuming is safe depends on what it already
*did*. Tests below assert that policy can never promote an unsafe run to resumable — that
inversion is the failure mode the design exists to prevent.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import Run, Task
from app.orchestrator import FAIL, RESUME, SKIP, classify_orphan


def _run(**kw) -> Run:
    base = dict(owner_id="local", task_id=1, trigger="manual", status="queued",
                started_at=None, provider=None)
    base.update(kw)
    return Run(**base)


def _task(policy: str = "next_occurrence", schedule_expr: str | None = None) -> Task:
    return Task(id=1, owner_id="local", name="t", prompt_template="x",
                recovery_policy=policy, schedule_expr=schedule_expr)


# ── layer 1: safety, decided from evidence ────────────────────────────────────

def test_queued_and_never_claimed_is_resumable():
    """The atomic claim never fired, so no executor ever touched this row."""
    action, reason = classify_orphan(_run(status="queued", started_at=None), _task())
    assert action == RESUME
    assert "never dispatched" in reason


def test_planning_without_a_provider_is_resumable():
    """Claimed, but routing/selection only — nothing external happened yet."""
    action, _ = classify_orphan(_run(status="planning", started_at=datetime.now(UTC)), _task())
    assert action == RESUME


def test_running_is_never_resumable():
    """`status` is committed immediately before the executor is invoked."""
    action, reason = classify_orphan(
        _run(status="running", started_at=datetime.now(UTC), provider="agy"), _task()
    )
    assert action == FAIL
    assert "agy" in reason and "dispatched" in reason


def test_planning_with_a_provider_already_chosen_is_not_resumable():
    """A provider on a `planning` row means dispatch was already under way."""
    action, _ = classify_orphan(
        _run(status="planning", started_at=datetime.now(UTC), provider="claude"), _task()
    )
    assert action == FAIL


def test_queued_with_a_start_time_is_treated_as_dispatched():
    """Defensive: a started_at on a `queued` row is an inconsistent state.

    It should never occur — the claim sets both together — but if it does, the safe
    reading is 'something already happened', not 'safe to re-run'.
    """
    action, _ = classify_orphan(_run(status="queued", started_at=datetime.now(UTC)), _task())
    assert action == FAIL


# ── layer 2: policy, and only for runs already known safe ─────────────────────

def test_scheduled_run_defaults_to_skipping_because_the_next_fire_is_the_recovery():
    action, reason = classify_orphan(
        _run(trigger="schedule"), _task("next_occurrence", schedule_expr="0 3 * * *")
    )
    assert action == SKIP
    assert "double-firing" in reason


def test_catch_up_resumes_a_scheduled_run():
    action, _ = classify_orphan(
        _run(trigger="schedule"), _task("catch_up", schedule_expr="0 3 * * *")
    )
    assert action == RESUME


def test_manual_runs_resume_regardless_of_policy():
    """A manual run has no 'next occurrence', so next_occurrence cannot apply to it."""
    action, _ = classify_orphan(
        _run(trigger="manual"), _task("next_occurrence", schedule_expr="0 3 * * *")
    )
    assert action == RESUME


def test_scheduled_run_on_a_task_with_no_schedule_resumes():
    """Skipping to 'the next fire' is only coherent when a next fire actually exists."""
    action, _ = classify_orphan(
        _run(trigger="schedule"), _task("next_occurrence", schedule_expr=None)
    )
    assert action == RESUME


@pytest.mark.parametrize("policy", ["next_occurrence", "catch_up"])
def test_policy_can_never_make_a_dispatched_run_resumable(policy):
    """The ordering guarantee: intent never overrides evidence.

    If this ever inverts, a task marked catch_up that died mid-tool-call would be re-run
    and repeat its side effects — exactly the failure D-0068 was written to prevent.
    """
    action, _ = classify_orphan(
        _run(trigger="schedule", status="running", started_at=datetime.now(UTC),
             provider="agy"),
        _task(policy, schedule_expr="0 3 * * *"),
    )
    assert action == FAIL


def test_missing_task_falls_back_to_the_conservative_policy():
    """A run whose task was deleted still classifies rather than raising."""
    action, _ = classify_orphan(_run(trigger="schedule"), None)
    # No task ⇒ no schedule known ⇒ nothing to defer to ⇒ safe to resume.
    assert action == RESUME
