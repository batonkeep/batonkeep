"""
test_telemetry.py — D-0022 Task A: the operational cockpit aggregator.

The cockpit is audience A (local-only, sovereign): it consolidates spend + run
outcomes + latency + failover + errors-by-class + build/session activity over a
window, owner-scoped, reusing the shipped primitives. These cover the dimensions
the cockpit adds beyond /api/stats + /api/usage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Owner, Run, Session, SessionTurn, Task
from app.telemetry import classify_error, operational_cockpit


@pytest.fixture
async def fresh_db(tmp_path):
    from app.db import Base
    import app.models  # noqa: F401 — register metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/tele.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="Test"))
        db.add(Owner(id="other", label="Other"))
        db.add(Task(id=1, owner_id="local", name="T", prompt_template="x"))
        await db.commit()
    yield Maker
    await engine.dispose()


def _now(ago_hours=0):
    return datetime.now(timezone.utc) - timedelta(hours=ago_hours)


async def _run(db, *, owner="local", status="succeeded", provider="claude", cost=0.0,
               trigger="manual", attempts=None, retry_count=0, overflow_used=False,
               error=None, duration_ms=None, ago_hours=0):
    started = _now(ago_hours)
    finished = None
    if duration_ms is not None:
        finished = started + timedelta(milliseconds=duration_ms)
    db.add(Run(
        owner_id=owner, task_id=1, status=status, provider=provider, cost_usd=cost,
        trigger=trigger, attempts=attempts, retry_count=retry_count,
        overflow_used=overflow_used, error=error,
        created_at=started, started_at=started, finished_at=finished,
    ))
    await db.commit()


class TestClassifyError:
    def test_rate_limit_from_text(self):
        assert classify_error(Run(status="failed", error="rate_limit_reached on claude")) == "rate_limited"

    def test_cooling(self):
        assert classify_error(Run(status="failed", error="All candidates cooling: ['claude']")) == "cooling"

    def test_interrupted_reaper(self):
        assert classify_error(Run(status="failed", error="interrupted by backend restart (reaped at startup)")) == "interrupted"

    def test_unavailable(self):
        assert classify_error(Run(status="failed", error="no candidates available")) == "unavailable"

    def test_falls_back_to_attempt_outcome(self):
        r = Run(status="failed", error="something vague",
                attempts=[{"provider": "a", "outcome": "rate_limited"}])
        assert classify_error(r) == "rate_limited"

    def test_generic_failed_is_error(self):
        assert classify_error(Run(status="failed", error="boom")) == "error"


class TestCockpit:
    @pytest.mark.asyncio
    async def test_run_outcomes_and_success_rate(self, fresh_db):
        async with fresh_db() as db:
            await _run(db, status="succeeded", provider="claude", cost=0.1)
            await _run(db, status="succeeded", provider="grok")
            await _run(db, status="failed", provider="claude", error="boom")
            c = await operational_cockpit(db, "local")
        assert c["runs"]["total"] == 3
        assert c["runs"]["by_status"]["succeeded"] == 2
        assert c["runs"]["by_status"]["failed"] == 1
        assert c["runs"]["success_rate"] == round(2 / 3, 4)
        assert c["runs"]["by_provider"]["claude"] == 2
        assert c["spend"]["spend_today_usd"] == 0.1

    @pytest.mark.asyncio
    async def test_owner_scoped(self, fresh_db):
        async with fresh_db() as db:
            await _run(db, owner="local")
            await _run(db, owner="other")
            c = await operational_cockpit(db, "local")
        assert c["runs"]["total"] == 1

    @pytest.mark.asyncio
    async def test_window_excludes_old_runs(self, fresh_db):
        async with fresh_db() as db:
            await _run(db, ago_hours=1)
            await _run(db, ago_hours=24 * 10)  # 10 days ago
            c = await operational_cockpit(db, "local", window_days=7)
        assert c["runs"]["total"] == 1

    @pytest.mark.asyncio
    async def test_latency_percentiles(self, fresh_db):
        async with fresh_db() as db:
            for d in (100, 200, 300, 400, 1000):
                await _run(db, duration_ms=d)
            c = await operational_cockpit(db, "local")
        lat = c["latency"]
        assert lat["sample"] == 5
        assert lat["p50_ms"] == 300
        assert lat["p95_ms"] == 1000
        assert lat["avg_ms"] == 400

    @pytest.mark.asyncio
    async def test_failover_and_reliability(self, fresh_db):
        async with fresh_db() as db:
            await _run(db, status="succeeded", attempts=[
                {"provider": "claude", "outcome": "rate_limited"},
                {"provider": "grok", "outcome": "success"},
            ])
            await _run(db, status="failed", error="boom", retry_count=2, overflow_used=True)
            c = await operational_cockpit(db, "local")
        rel = c["reliability"]
        assert rel["failover_rate"] == round(1 / 2, 4)
        assert rel["failover_reasons"]["rate_limited"] == 1
        assert rel["retried_runs"] == 1
        assert rel["budget_degraded_runs"] == 1

    @pytest.mark.asyncio
    async def test_errors_by_class(self, fresh_db):
        async with fresh_db() as db:
            await _run(db, status="failed", error="rate_limit_reached")
            await _run(db, status="failed", error="All candidates cooling: []")
            await _run(db, status="failed", error="boom")
            c = await operational_cockpit(db, "local")
        assert c["errors_by_class"]["rate_limited"] == 1
        assert c["errors_by_class"]["cooling"] == 1
        assert c["errors_by_class"]["error"] == 1
        assert c["runs"]["error_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_build_session_activity(self, fresh_db):
        async with fresh_db() as db:
            db.add(Session(id="s1", owner_id="local", title="A", workspace_path="/w/1",
                           status="active", confidential=True, created_at=_now(1)))
            db.add(Session(id="s2", owner_id="local", title="B", workspace_path="/w/2",
                           status="archived", created_at=_now(2)))
            db.add(Session(id="s3", owner_id="other", title="C", workspace_path="/w/3",
                           created_at=_now(1)))
            await db.commit()
            db.add(SessionTurn(session_id="s1", owner_id="local", seq=1, prompt="x",
                               status="succeeded", created_at=_now(1)))
            db.add(SessionTurn(session_id="s1", owner_id="local", seq=2, prompt="y",
                               status="failed", created_at=_now(1)))
            await db.commit()
            c = await operational_cockpit(db, "local")
        act = c["activity"]
        assert act["sessions_total"] == 2  # owner-scoped, excludes "other"
        assert act["sessions_active"] == 1
        assert act["sessions_archived"] == 1
        assert act["sessions_confidential"] == 1
        assert act["turns_total"] == 2
        assert act["turns_by_status"]["succeeded"] == 1
        assert act["turns_by_status"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_window_clamped(self, fresh_db):
        async with fresh_db() as db:
            c = await operational_cockpit(db, "local", window_days=9999)
        assert c["window_days"] == 90
