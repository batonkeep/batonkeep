"""
telemetry.py — the operational cockpit aggregator (D-0022, Task A; resolves P-0024).

This is **audience A** from the telemetry posture: a *local-first, sovereign-by-
construction* view for the user to monitor **their own** work — spend, run
outcomes, failovers, latency, subscription-quota %, and session/build activity.
It lives entirely in the user's own deployment, answers from data already on the
box, and **never leaves it**. There is no consent question here — that is audience
B (opt-in, content-free product analytics; gated on managed), which this module
deliberately does NOT touch.

Per D-0022 this is **one** telemetry model, not a parallel pipe: it reuses the
already-shipped primitives — `cost.usage_summary` (P-0009 #2), the `Run`/`Session`
domain telemetry, and the best-effort `quota_tracker` health — and adds only the
dimensions the cockpit needs that no single surface had yet: a configurable time
window, latency percentiles, error-by-class, failover-reason breakdown, and
session/build activity.

Owner-scoped on every query (P3). Read-only aggregation; collects no new data.

API:
    operational_cockpit(db, owner_id, window_days=7) -> dict
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cost import usage_summary
from app.models import Run, Session, SessionTurn

# Terminal run statuses (a run that has stopped producing).
_TERMINAL = ("succeeded", "failed")


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list (pct in 0..1)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    # nearest-rank: rank = ceil(pct * N), clamped to [1, N]
    rank = max(1, min(len(sorted_vals), int(-(-pct * len(sorted_vals) // 1))))
    return sorted_vals[rank - 1]


def classify_error(run: Run) -> str:
    """
    Map a failed run to a coarse **error class** for the cockpit's error-rate view.

    Classes are structural, not message text (the cockpit shows counts by class,
    never raw error strings — that keeps it shareable as the same shape audience B
    would aggregate). Derived from the run's terminal `error` text + the per-attempt
    outcome log (the grounded vocab: rate_limited | unavailable | error).
    """
    err = (run.error or "").lower()
    if "interrupted by backend restart" in err or "reaped" in err:
        return "interrupted"
    if "timed out" in err or "timeout" in err:
        return "timed_out"
    if "cooling" in err:
        return "cooling"
    if "rate" in err and "limit" in err:
        return "rate_limited"
    # Empty-output nonzero exit (P-0069 item 4): the near-silent handoff failure —
    # the agent produced no terminal result (only reasoning/thoughts, or a nonzero
    # exit with no text). A distinct class so it stops hiding inside generic "error".
    if "without output" in err or "without producing" in err:
        return "empty_output"
    if "unavailable" in err or "no candidates" in err or "no provider" in err:
        return "unavailable"

    # Fall back to the dominant per-attempt outcome when the terminal text is vague.
    outcomes = [a.get("outcome") for a in (run.attempts or []) if isinstance(a, dict)]
    for cls in ("rate_limited", "unavailable", "error"):
        if cls in outcomes:
            return cls if cls != "error" else "error"
    return "error" if run.status == "failed" else "other"


async def operational_cockpit(
    db: AsyncSession, owner_id: str, window_days: int = 7
) -> dict:
    """
    The consolidated operator cockpit (audience A). All figures are owner-scoped
    and computed over the trailing `window_days` window (plus today/7d cost from
    the existing budget surface). Local-only; nothing here is shared.
    """
    window_days = max(1, min(window_days, 90))
    now = datetime.now(UTC)
    since = now - timedelta(days=window_days)

    # ── Spend (reuse the shipped cost model; today / 7d / by-provider / budget) ──
    spend = await usage_summary(db, owner_id)

    # ── Runs in the window ──────────────────────────────────────────────────────
    runs = (await db.execute(
        select(Run).where(Run.owner_id == owner_id, Run.created_at >= since)
    )).scalars().all()

    by_status: Counter[str] = Counter(r.status for r in runs)
    by_provider: Counter[str] = Counter(r.provider for r in runs if r.provider)
    by_trigger: Counter[str] = Counter(r.trigger for r in runs)

    terminal = [r for r in runs if r.status in _TERMINAL]
    succeeded = [r for r in runs if r.status == "succeeded"]
    failed = [r for r in runs if r.status == "failed"]
    success_rate = (len(succeeded) / len(terminal)) if terminal else 0.0

    # ── Latency (succeeded runs; avg + p50/p95) ─────────────────────────────────
    durations = sorted(
        r.duration_ms for r in succeeded if r.duration_ms is not None
    )
    latency = {
        "avg_ms": (sum(durations) / len(durations)) if durations else None,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "sample": len(durations),
    }

    # ── Reliability: failover + retries + degrade ───────────────────────────────
    failed_over = sum(1 for r in terminal if r.attempts and len(r.attempts) > 1)
    failover_rate = (failed_over / len(terminal)) if terminal else 0.0
    # Why failovers happened: count the non-final attempt outcomes across runs.
    failover_reasons: Counter[str] = Counter()
    for r in runs:
        attempts = r.attempts or []
        for a in attempts[:-1] if len(attempts) > 1 else []:
            if isinstance(a, dict) and a.get("outcome"):
                failover_reasons[a["outcome"]] += 1
    retried_runs = sum(1 for r in runs if (r.retry_count or 0) > 0)
    budget_degraded_runs = sum(1 for r in runs if r.overflow_used)

    # ── Errors by class (structural, content-free) ──────────────────────────────
    errors_by_class: Counter[str] = Counter(classify_error(r) for r in failed)
    error_rate = (len(failed) / len(terminal)) if terminal else 0.0

    # ── Live snapshot (owner-global, not windowed) ──────────────────────────────
    deferred_now = await db.scalar(
        select(func.count(Run.id)).where(
            Run.owner_id == owner_id, Run.status == "deferred"
        )
    ) or 0
    active_runs = await db.scalar(
        select(func.count(Run.id)).where(
            Run.owner_id == owner_id,
            Run.status.in_(("queued", "planning", "running")),
        )
    ) or 0

    # ── Build/session activity (windowed) ───────────────────────────────────────
    sessions = (await db.execute(
        select(Session).where(Session.owner_id == owner_id, Session.created_at >= since)
    )).scalars().all()
    sessions_active = sum(1 for s in sessions if s.status == "active")
    sessions_archived = sum(1 for s in sessions if s.status == "archived")
    sessions_confidential = sum(1 for s in sessions if s.confidential)

    turn_rows = (await db.execute(
        select(SessionTurn.status, func.count(SessionTurn.id))
        .where(SessionTurn.owner_id == owner_id, SessionTurn.created_at >= since)
        .group_by(SessionTurn.status)
    )).all()
    turns_by_status = {(st or "unknown"): int(c) for st, c in turn_rows}
    turns_total = sum(turns_by_status.values())

    return {
        "window_days": window_days,
        "since": since,
        "generated_at": now,
        "spend": spend,
        "runs": {
            "total": len(runs),
            "by_status": dict(by_status),
            "by_provider": dict(by_provider),
            "by_trigger": dict(by_trigger),
            "success_rate": round(success_rate, 4),
            "error_rate": round(error_rate, 4),
            "deferred_now": int(deferred_now),
            "active_runs": int(active_runs),
        },
        "latency": latency,
        "reliability": {
            "failover_rate": round(failover_rate, 4),
            "failover_reasons": dict(failover_reasons),
            "retried_runs": retried_runs,
            "budget_degraded_runs": budget_degraded_runs,
        },
        "errors_by_class": dict(errors_by_class),
        "activity": {
            "sessions_total": len(sessions),
            "sessions_active": sessions_active,
            "sessions_archived": sessions_archived,
            "sessions_confidential": sessions_confidential,
            "turns_total": turns_total,
            "turns_by_status": turns_by_status,
        },
    }
