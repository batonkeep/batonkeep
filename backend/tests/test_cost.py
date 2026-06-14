"""
test_cost.py — P-0009 #2: spend aggregation + budget gate.

- spend_since / usage_summary aggregate Run.cost_usd over windows + by provider
- over_daily_budget honours the configured cap (0 = unlimited)
- is_free_provider classifies plan-CLI + local as zero-marginal-cost
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Owner, Run, Session, SessionTurn, Task


@pytest.fixture
async def fresh_db(tmp_path):
    import app.models  # noqa: F401 — register metadata
    from app.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cost.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add(Owner(id="local", label="Test"))
        db.add(Task(id=1, owner_id="local", name="T", prompt_template="x"))
        await db.commit()
    yield Session
    await engine.dispose()


async def _add_run(db, *, provider, cost, ago_hours=0):
    db.add(Run(
        owner_id="local", task_id=1, status="succeeded", provider=provider,
        cost_usd=cost,
        created_at=datetime.now(UTC) - timedelta(hours=ago_hours),
    ))
    await db.commit()


async def _add_session_turn(db, *, provider, cost, ago_hours=0):
    sid = f"sess-{provider}-{cost}"
    db.add(Session(id=sid, owner_id="local", workspace_path="/tmp/x"))
    db.add(SessionTurn(
        session_id=sid, owner_id="local", seq=0, provider=provider,
        status="succeeded", cost_usd=cost,
        created_at=datetime.now(UTC) - timedelta(hours=ago_hours),
    ))
    await db.commit()


class TestAggregation:
    @pytest.mark.asyncio
    async def test_usage_summary_sums_today_and_by_provider(self, fresh_db):
        from app.cost import usage_summary
        async with fresh_db() as db:
            await _add_run(db, provider="openai-api", cost=0.10)
            await _add_run(db, provider="openai-api", cost=0.05)
            await _add_run(db, provider="claude", cost=0.0)
            s = await usage_summary(db, "local")
        assert round(s["spend_today_usd"], 2) == 0.15
        assert s["by_provider_today"]["openai-api"] == 0.15
        assert s["by_provider_today"]["claude"] == 0.0

    @pytest.mark.asyncio
    async def test_build_session_spend_counts(self, fresh_db):
        # Note 2: build-session turns are real metered spend and must be summed.
        from app.cost import usage_summary
        async with fresh_db() as db:
            await _add_run(db, provider="openai-api", cost=0.10)
            await _add_session_turn(db, provider="openai-api", cost=0.07)
            s = await usage_summary(db, "local")
        assert round(s["spend_today_usd"], 2) == 0.17           # run + session turn
        assert s["by_provider_today"]["openai-api"] == 0.17     # merged by provider

    @pytest.mark.asyncio
    async def test_7d_window_includes_recent_excludes_old(self, fresh_db):
        from app.cost import usage_summary
        async with fresh_db() as db:
            await _add_run(db, provider="openai-api", cost=1.00, ago_hours=24 * 3)   # in 7d
            await _add_run(db, provider="openai-api", cost=2.00, ago_hours=24 * 10)  # outside 7d
            s = await usage_summary(db, "local")
        assert round(s["spend_7d_usd"], 2) == 1.00


class TestBudgetGate:
    @pytest.mark.asyncio
    async def test_unlimited_when_cap_zero(self, fresh_db):
        from app.config import get_settings
        from app.cost import over_daily_budget
        settings = get_settings()
        settings.__dict__["daily_budget_usd"] = 0.0
        async with fresh_db() as db:
            await _add_run(db, provider="openai-api", cost=999.0)
            assert await over_daily_budget(db, "local") is False

    @pytest.mark.asyncio
    async def test_over_budget_when_spend_reaches_cap(self, fresh_db):
        from app.config import get_settings
        from app.cost import over_daily_budget, usage_summary
        settings = get_settings()
        orig = settings.daily_budget_usd
        settings.__dict__["daily_budget_usd"] = 0.50
        try:
            async with fresh_db() as db:
                await _add_run(db, provider="openai-api", cost=0.40)
                assert await over_daily_budget(db, "local") is False
                await _add_run(db, provider="openai-api", cost=0.15)  # now 0.55 ≥ 0.50
                assert await over_daily_budget(db, "local") is True
                s = await usage_summary(db, "local")
                assert s["over_budget"] is True
                assert s["remaining_today_usd"] == 0.0
        finally:
            settings.__dict__["daily_budget_usd"] = orig


class TestFreeProvider:
    def test_classification(self):
        from app.cost import is_free_provider
        from app.providers.registry import get_provider_def
        assert is_free_provider(get_provider_def("claude")) is True       # plan-CLI
        assert is_free_provider(get_provider_def("ollama")) is True       # local
        assert is_free_provider(get_provider_def("openai-api")) is False  # paid API
