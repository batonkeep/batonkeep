"""P-0100 / Gate C — the cross-lane activity feed.

The exit criterion is that an operator can answer, from the UI alone, *what each agent
did, what it produced, what it got wrong, and what is waiting on them*. These tests pin
the two properties that make the answer honest rather than merely present.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/act.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            await db.commit()
        return Maker

    Maker = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app), Maker
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def _seed(Maker, **kw):
    from app.models import Approval, PlannerRun, Project, Run, Task

    async def _go():
        now = datetime.now(UTC)
        async with Maker() as db:
            db.add(Project(id="p1", owner_id="local", name="Proj"))
            task = Task(owner_id="local", name="Daily Brief", prompt_template="x")
            db.add(task)
            await db.commit()

            # A run that reports success but delivered nothing (P-0069 advisory).
            db.add(Run(owner_id="local", task_id=task.id, status="succeeded",
                       provider="agy", output_flags={"outputs_missing": True},
                       created_at=now - timedelta(minutes=5)))
            # A clean run.
            db.add(Run(owner_id="local", task_id=task.id, status="succeeded",
                       provider="agy", markdown_path="/data/outputs/run_2/output.md",
                       cost_usd=0.25, created_at=now - timedelta(minutes=4)))
            # A run parked on a decision.
            parked = Run(owner_id="local", task_id=task.id, status="parked",
                         provider="claude-api", created_at=now - timedelta(minutes=3))
            db.add(parked)
            await db.commit()
            db.add(Approval(owner_id="local", request_id="r1", kind="code_exec",
                            status="pending", run_id=parked.id, producer="claude-api"))
            db.add(PlannerRun(owner_id="local", project_id="p1", status="no_proposals",
                              provider="grok", created_at=now - timedelta(minutes=2)))
            await db.commit()
            return parked.id

    return asyncio.get_event_loop().run_until_complete(_go())


def test_feed_merges_lanes_newest_first(client):
    c, Maker = client
    _seed(Maker)
    rows = c.get("/api/activity").json()

    assert [r["kind"] for r in rows] == ["planner", "run", "run", "run"], (
        "one timeline across lanes, newest first — three separate views is the "
        "fragmentation Gate C exists to remove"
    )
    # The operator recognises work by name, not id.
    assert {r["actor"] for r in rows if r["kind"] == "run"} == {"Daily Brief"}


def test_outcome_is_the_work_outcome_not_the_transport_status(client):
    """The property that makes the feed honest (C3 / P-0070)."""
    c, Maker = client
    _seed(Maker)
    rows = c.get("/api/activity").json()

    missing = next(r for r in rows if r["status"] == "succeeded" and r["cost_usd"] == 0.0)
    assert missing["outcome"] == "outputs_missing", (
        "a run that delivered nothing must not read as a success in the feed"
    )
    assert missing["status"] == "succeeded", (
        "the transport status stays visible — the two disagreeing is the information"
    )

    clean = next(r for r in rows if r["cost_usd"] == 0.25)
    assert clean["outcome"] == "succeeded"
    assert clean["artifact"] == "/data/outputs/run_2/output.md", "what it produced"


def test_an_item_waiting_on_the_operator_says_so(client):
    """"What is waiting on them" is answered in the same place as "what happened"."""
    c, Maker = client
    parked_id = _seed(Maker)
    rows = c.get("/api/activity").json()

    parked = next(r for r in rows if r["kind"] == "run" and r["id"] == parked_id)
    assert parked["status"] == "parked"
    assert parked["awaiting_approval_id"] is not None, (
        "a parked run must be actionable from the feed, not merely look stalled"
    )


def test_planner_outcomes_pass_through_unclassified(client):
    """P-0080 already made the planner's terminal states *work* outcomes: `no_proposals`
    is a clean result, not a failure, and must not be re-graded here."""
    c, Maker = client
    _seed(Maker)
    rows = c.get("/api/activity").json()
    planner = next(r for r in rows if r["kind"] == "planner")
    assert planner["outcome"] == "no_proposals"


def test_limit_is_bounded(client):
    c, Maker = client
    _seed(Maker)
    assert len(c.get("/api/activity?limit=2").json()) == 2
    # An unbounded feed is a denial-of-service on the operator's own instance.
    assert len(c.get("/api/activity?limit=99999").json()) <= 200
