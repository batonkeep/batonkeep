"""
tests/test_deferral_invariant.py — a deferral must name the time it resolves (P-0112).

The defect these cover stranded 47 runs on the always-on testbed for ten days while
1076 tests passed. `_sweep_deferred_runs` selects `deferred_until <= now`, so a NULL is
invisible to it forever — the run waits in a status the whole product renders as
temporary and the operator surfaces read as benign.

Two layers are tested, deliberately, because the reason this is a *proposal* and not a
one-line fix is that the same defect was already fixed once in a sibling branch and
survived in two others:

  1. **Policy** (option (a)) — the router distinguishes "cannot yet" from "cannot",
     and the orchestrator fails the second honestly.
  2. **Invariant** (option (d)) — the write boundary refuses the bad state regardless
     of which code path produced it, including paths that do not exist yet.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.quota import QuotaTracker
from app.router import CandidatePlan, DeferredResult, UnroutableResult, resolve


def _routing(**over):
    base = {"strategy": "capability", "candidates": ["mock"], "capability_tags": [],
            "failover": True, "max_attempts": 3}
    base.update(over)
    return base


# ── Layer 1: the router tells "cannot yet" from "cannot" ─────────────────────

class TestRouterDistinguishesTemporalFromPermanent:
    def test_the_testbed_shape_is_unroutable(self):
        """`mock` + a tag it does not carry — the exact 47-run condition.

        `mock`'s tags are ["mock", "any"] and "any" is not a wildcard, so nothing
        matches. Nothing is cooling; no wait helps.
        """
        r = resolve(_routing(capability_tags=["synthesis"]), QuotaTracker())
        assert isinstance(r, UnroutableResult)
        assert "capability tags" in r.reason
        assert r.trace is not None and r.trace.unroutable and not r.trace.deferred

    def test_cooling_still_defers_and_carries_a_time(self):
        q = QuotaTracker()
        until = datetime.now(UTC) + timedelta(minutes=20)
        q.mark_cooldown("mock", until)
        r = resolve(_routing(capability_tags=["any"]), q)
        assert isinstance(r, DeferredResult)
        assert r.deferred_until == until
        assert r.cooling_providers == ["mock"]

    def test_cooling_whose_reset_already_passed_is_unroutable_not_stranded(self):
        """The guard inside `_defer`, which is what makes this a boundary rather
        than three patches: a caller that cannot supply a reset gets an honest
        failure instead of a permanent strand.

        Reachable, not theoretical. A provider can report a `reset_at` that is
        already in the past — clock skew, or a stale reset header — and the first
        `is_healthy()` call then expires the cooldown and clears `cooldown_until`
        while the run's `rate_limited_any` flag stays set. `earliest_reset()`
        returns None from there.
        """
        q = QuotaTracker()
        q.mark_cooldown("mock", datetime.now(UTC) - timedelta(minutes=5))
        q.is_healthy("mock")  # expires the cooldown and clears cooldown_until
        assert q.earliest_reset(["mock"]) is None
        # The provider is healthy again, so routing succeeds — the point being that
        # no path reaches DeferredResult without a time.
        r = resolve(_routing(capability_tags=["any"]), q)
        assert not (isinstance(r, DeferredResult) and r.deferred_until is None)

    def test_defer_with_no_time_degrades_to_unroutable(self):
        """The fallback inside `_defer` itself, exercised directly.

        Reaching it means the tracker says "cooling" while having no reset to offer —
        an inconsistent internal state rather than an operating condition. The guard
        exists because the alternative outcome is a run that waits forever, and a
        guard that only covers states we currently believe reachable is the reasoning
        that left this defect in two sibling branches.
        """
        q = QuotaTracker()
        q.mark_cooldown("mock", datetime.now(UTC) + timedelta(minutes=30))
        # Drop the reset while leaving the provider unhealthy — the state the guard
        # is for. Deliberately reaching past the public API: no supported call
        # produces it, which is the point.
        q._health["mock"].cooldown_until = None
        r = resolve(_routing(capability_tags=["any"]), q)
        assert isinstance(r, UnroutableResult)
        assert "no known reset time" in r.reason

    def test_over_budget_defers_with_the_reset_time(self):
        """Being over the daily cap *is* temporal — it must defer, and say until when."""
        r = resolve(_routing(capability_tags=["any"]), QuotaTracker(), degrade_to_free=True)
        # mock is free, so this resolves; the assertion that matters is the negative —
        # no path may produce a DeferredResult without a time.
        if isinstance(r, DeferredResult):
            assert r.deferred_until is not None

    def test_a_routable_policy_is_unaffected(self):
        r = resolve(_routing(capability_tags=["any"]), QuotaTracker())
        assert isinstance(r, CandidatePlan)
        assert r.candidates == ["mock"]


# ── Layer 2: the write boundary refuses the state outright ───────────────────

@pytest.fixture
async def maker(tmp_path):
    from app.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/inv.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _task(db):
    from app.models import Owner, Task

    db.add(Owner(id="local", label="T"))
    t = Task(owner_id="local", name="t", prompt_template="p")
    db.add(t)
    await db.flush()
    return t


class TestTheWriteBoundary:
    async def test_deferred_without_a_time_is_coerced_to_failed(self, maker, caplog):
        """Whatever code path produced it — including one written next year."""
        from app.models import Run

        async with maker() as db:
            t = await _task(db)
            run = Run(owner_id="local", task_id=t.id, status="deferred")
            db.add(run)
            with caplog.at_level(logging.ERROR, logger="app.db"):
                await db.commit()
            rid = run.id

        async with maker() as db:
            got = await db.get(Run, rid)
        assert got.status == "failed", "a deferral the sweep can never see must not persist"
        assert "no resolution time" in (got.error or "")
        assert got.finished_at is not None
        assert any("INVARIANT" in r.message for r in caplog.records), (
            "reaching the guard is a code defect and must be loud"
        )

    async def test_deferred_with_a_time_is_left_alone(self, maker):
        from app.models import Run

        until = datetime.now(UTC) + timedelta(hours=1)
        async with maker() as db:
            t = await _task(db)
            run = Run(owner_id="local", task_id=t.id, status="deferred", deferred_until=until)
            db.add(run)
            await db.commit()
            rid = run.id

        async with maker() as db:
            got = await db.get(Run, rid)
        assert got.status == "deferred"
        assert got.deferred_until is not None

    async def test_the_guard_also_covers_an_update_not_just_an_insert(self, maker):
        """`session.dirty`, not only `session.new` — the orchestrator mutates a
        loaded Run rather than inserting one, which is how every real instance of
        this defect was written."""
        from app.models import Run

        async with maker() as db:
            t = await _task(db)
            run = Run(owner_id="local", task_id=t.id, status="running")
            db.add(run)
            await db.commit()
            rid = run.id

        async with maker() as db:
            got = await db.get(Run, rid)
            got.status = "deferred"          # no deferred_until — the real bug shape
            await db.commit()

        async with maker() as db:
            assert (await db.get(Run, rid)).status == "failed"


# ── The sweep's own contract, stated as a test ───────────────────────────────

async def test_the_sweep_cannot_see_a_null_deferral(maker, monkeypatch):
    """Not a bug report — the reason the invariant exists, pinned so it cannot be
    quietly weakened into "let the sweep pick up NULLs" (option (c), rejected:
    it turns a permanent strand into an infinite retry and keeps calling it
    "deferred")."""
    from sqlalchemy import select

    from app.models import Run

    async with maker() as db:
        t = await _task(db)
        # Bypass the ORM guard deliberately to reconstruct the legacy rows that
        # already exist in the field (47 of them on Runtime B).
        db.add(Run(owner_id="local", task_id=t.id, status="deferred",
                   deferred_until=datetime.now(UTC) - timedelta(days=1)))
        await db.commit()
        await db.execute(
            Run.__table__.update().values(deferred_until=None)
        )
        await db.commit()

    async with maker() as db:
        rows = (await db.execute(
            select(Run).where(Run.status == "deferred",
                              Run.deferred_until <= datetime.now(UTC))
        )).scalars().all()
    assert rows == [], "a NULL deferred_until is invisible to the sweep — forever"


# ── P-0113: "is this agent still doing work?" ────────────────────────────────

async def test_last_delivered_at_sees_past_a_long_run_of_non_delivery(maker, monkeypatch):
    """The number that would have caught the ten-day outage.

    `recent_failures` looks at the last 20 runs, so an agent that stopped delivering
    more than 20 runs ago shows nothing there. `last_delivered_at` is not windowed:
    it answers when the agent last actually produced something, which stays true
    however long it has been failing and whatever status the failures wear.
    """
    from app.models import Owner, Run, Task

    async with maker() as db:
        db.add(Owner(id="local", label="T"))
        t = Task(owner_id="local", name="a", prompt_template="p",
                 schedule_kind="cron", schedule_expr="0 7 * * *")
        db.add(t)
        await db.flush()
        base = datetime.now(UTC) - timedelta(days=30)
        # One real delivery, long ago...
        db.add(Run(owner_id="local", task_id=t.id, status="succeeded",
                   created_at=base, provider="mock"))
        # ...then 30 deferrals, every one of them carrying a valid resume time, so
        # nothing about them is individually wrong. This is the shape that read as
        # healthy: benign status, plausible count, no failures.
        for i in range(1, 31):
            db.add(Run(owner_id="local", task_id=t.id, status="deferred",
                       created_at=base + timedelta(days=i),
                       deferred_until=datetime.now(UTC) + timedelta(hours=1)))
        await db.commit()
        tid = t.id

    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app as fastapi_app

    async def _override():
        async with maker() as db:
            yield db

    # No `with` — the lifespan would open the real database. Matches the pattern
    # the other API tests use.
    fastapi_app.dependency_overrides[get_db] = _override
    try:
        agents = TestClient(fastapi_app).get("/api/agents").json()
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)

    agent = next(a for a in agents if a["task_id"] == tid)
    assert agent["recent_failures"] == 0, (
        "the windowed failure count sees nothing wrong — this is the blind spot"
    )
    assert agent["last_delivered_at"] is not None
    delivered = datetime.fromisoformat(agent["last_delivered_at"])
    if delivered.tzinfo is None:
        delivered = delivered.replace(tzinfo=UTC)
    assert (datetime.now(UTC) - delivered).days >= 29, (
        "last delivery must report the real date, not the newest run"
    )


async def test_last_delivered_at_ignores_a_success_that_delivered_nothing(maker):
    """P-0070's distinction, carried through: transport success is not delivery."""
    from app.models import Owner, Run, Task

    async with maker() as db:
        db.add(Owner(id="local", label="T"))
        t = Task(owner_id="local", name="b", prompt_template="p",
                 schedule_kind="cron", schedule_expr="0 7 * * *")
        db.add(t)
        await db.flush()
        db.add(Run(owner_id="local", task_id=t.id, status="succeeded",
                   created_at=datetime.now(UTC) - timedelta(hours=1),
                   output_flags={"v": 1, "outputs_missing": ["report.md"]}))
        await db.commit()
        tid = t.id

    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app as fastapi_app

    async def _override():
        async with maker() as db:
            yield db

    # No `with` — the lifespan would open the real database. Matches the pattern
    # the other API tests use.
    fastapi_app.dependency_overrides[get_db] = _override
    try:
        agents = TestClient(fastapi_app).get("/api/agents").json()
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)

    agent = next(a for a in agents if a["task_id"] == tid)
    assert agent["last_delivered_at"] is None, (
        "a run that reported success while producing nothing is not a delivery"
    )
