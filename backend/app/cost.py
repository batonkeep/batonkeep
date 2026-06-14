"""
cost.py — Spend aggregation + budget surface (P-0009 #2).

Per-run cost is already metered on Run.cost_usd by the orchestrator. This module
aggregates it into an owner-level view and answers the one question the budget
gate needs: "has today's spend reached the daily cap?"

Graceful degradation, not denial: when over budget the router falls back to
zero-marginal-cost providers — subscription plan-CLIs (we pay a flat sub, not
per token) and local models (inference on our own box) — and defers only if none
are available. is_free_provider() defines that set.

API:
    spend_since(db, owner_id, since) -> float
    usage_summary(db, owner_id) -> dict        # today / 7d / by_provider / budget
    is_free_provider(pdef) -> bool             # zero marginal $ (cli or local)
    over_daily_budget(db, owner_id) -> bool
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Run, SessionTurn
from app.providers.registry import ProviderDef


def is_free_provider(pdef: ProviderDef) -> bool:
    """
    True iff a provider has **zero marginal cost per run**: a plan-CLI (flat
    subscription) or a local model (our own hardware). These are the budget
    fallback — using them more doesn't grow metered spend.
    """
    return pdef.kind == "cli" or pdef.local


def _start_of_today() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def spend_since(db: AsyncSession, owner_id: str, since: datetime) -> float:
    """Total metered USD spend for an owner since `since` (inclusive).

    Sums both **task runs** (`Run`) and **build-session turns** (`SessionTurn`) —
    build sessions on the API path are real metered spend and must count toward
    the surface + the budget gate (previously only `Run` was summed → sessions
    silently showed $0)."""
    run_total = await db.scalar(
        select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(
            Run.owner_id == owner_id,
            Run.created_at >= since,
        )
    )
    session_total = await db.scalar(
        select(func.coalesce(func.sum(SessionTurn.cost_usd), 0.0)).where(
            SessionTurn.owner_id == owner_id,
            SessionTurn.created_at >= since,
        )
    )
    return float(run_total or 0.0) + float(session_total or 0.0)


async def over_daily_budget(db: AsyncSession, owner_id: str) -> bool:
    """
    Whether today's spend has reached the configured daily cap. Always False when
    the cap is 0 (unlimited) — the common dev/OSS default.
    """
    cap = get_settings().daily_budget_usd
    if cap <= 0:
        return False
    return await spend_since(db, owner_id, _start_of_today()) >= cap


async def usage_summary(db: AsyncSession, owner_id: str) -> dict:
    """
    The named cost surface (P-0009 #2): today / last-7-day spend, per-provider
    breakdown, the configured cap, remaining headroom, and the degrade flag.
    API + log, not a dashboard.
    """
    cap = get_settings().daily_budget_usd
    today_start = _start_of_today()
    week_start = today_start - timedelta(days=6)  # today + 6 prior days = 7-day window

    spend_today = await spend_since(db, owner_id, today_start)
    spend_7d = await spend_since(db, owner_id, week_start)

    by_provider: dict[str, float] = {}
    for prov_col, cost_col, created_col, owner_col in (
        (Run.provider, Run.cost_usd, Run.created_at, Run.owner_id),
        (SessionTurn.provider, SessionTurn.cost_usd,
         SessionTurn.created_at, SessionTurn.owner_id),
    ):
        rows = (await db.execute(
            select(prov_col, func.coalesce(func.sum(cost_col), 0.0))
            .where(owner_col == owner_id, created_col >= today_start)
            .group_by(prov_col)
        )).all()
        for prov, c in rows:
            key = prov or "unknown"
            by_provider[key] = round(by_provider.get(key, 0.0) + float(c or 0.0), 6)

    over = cap > 0 and spend_today >= cap
    remaining = max(0.0, cap - spend_today) if cap > 0 else None

    return {
        "spend_today_usd": round(spend_today, 6),
        "spend_7d_usd": round(spend_7d, 6),
        "by_provider_today": by_provider,
        "daily_budget_usd": cap,            # 0 = unlimited
        "remaining_today_usd": remaining,   # None when unlimited
        "over_budget": over,
    }
