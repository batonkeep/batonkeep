"""D-0070 — an agent may propose a schedule; only the operator may grant it.

This is the one agent capability that would otherwise escape the proposer-only boundary:
every other planner tool proposes something the operator accepts *one at a time*, whereas
a schedule grants recurring, unattended, future execution **once, for all firings**.

So the property under test is not "a schedule can be created" — it is that **nothing
exists until a human says so**, and that the human, not the model, sets the cadence.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner, Project

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/sched.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="Proj"))
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


def _propose(Maker, monkeypatch, **kw):
    from app.providers.tools import planner_tools

    monkeypatch.setattr(planner_tools, "AsyncSessionLocal", Maker)
    import app.approvals as approvals_mod
    monkeypatch.setattr(approvals_mod, "AsyncSessionLocal", Maker, raising=False)

    async def _go():
        return await planner_tools.propose_schedule(
            kw.get("name", "Daily Brief"),
            kw.get("prompt", "Summarise overnight signals."),
            kw.get("cadence", "every weekday at 07:00"),
            rationale=kw.get("rationale", "the signal arrives daily"),
            context={"owner_id": "local", "project_id": "p1", "run_id": None},
        )

    return asyncio.get_event_loop().run_until_complete(_go())


def test_proposing_creates_no_task_at_all(client, monkeypatch):
    """The whole point: proposing must not grant anything."""
    c, Maker = client
    out = _propose(Maker, monkeypatch)
    assert "pending the operator's approval" in out

    from app.models import Approval, Task

    async def _check():
        async with Maker() as db:
            from sqlalchemy import select
            tasks = (await db.execute(select(Task))).scalars().all()
            appr = (await db.execute(select(Approval))).scalars().all()
        return tasks, appr

    tasks, appr = asyncio.get_event_loop().run_until_complete(_check())
    assert tasks == [], "a proposed schedule must create no Task — nothing is granted yet"
    assert len(appr) == 1
    assert appr[0].kind == "schedule_proposal" and appr[0].status == "pending"
    # The agent's intent is recorded in words; it did not get to write cron.
    assert appr[0].payload["cadence"] == "every weekday at 07:00"
    assert "cron" not in appr[0].payload


def test_approval_requires_the_operator_to_set_the_schedule(client, monkeypatch):
    """The cadence in words is intent; the firing pattern is the authority being granted."""
    c, Maker = client
    _propose(Maker, monkeypatch)
    r = c.post("/api/approvals/1/decide", json={"approved": True})
    assert r.status_code == 400
    assert "schedule_expr" in r.json()["detail"]


def test_approving_grants_it_and_only_then(client, monkeypatch):
    c, Maker = client
    _propose(Maker, monkeypatch)
    r = c.post("/api/approvals/1/decide",
               json={"approved": True, "schedule_expr": "0 7 * * 1-5"})
    assert r.status_code == 200
    body = r.json()
    assert body["approval"]["status"] == "approved"
    assert body["applied"]["schedule_expr"] == "0 7 * * 1-5"

    from sqlalchemy import select

    from app.models import Task

    async def _check():
        async with Maker() as db:
            return (await db.execute(select(Task))).scalars().all()

    tasks = asyncio.get_event_loop().run_until_complete(_check())
    assert len(tasks) == 1
    assert tasks[0].name == "Daily Brief"
    assert tasks[0].schedule_expr == "0 7 * * 1-5", "the operator's cadence, not the agent's"
    assert tasks[0].enabled is True


def test_denying_leaves_nothing_behind(client, monkeypatch):
    """A denied proposal must not leave a disabled Task lying around to be enabled later."""
    c, Maker = client
    _propose(Maker, monkeypatch)
    r = c.post("/api/approvals/1/decide", json={"approved": False})
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "denied"

    from sqlalchemy import select

    from app.models import Task

    async def _check():
        async with Maker() as db:
            return (await db.execute(select(Task))).scalars().all()

    assert asyncio.get_event_loop().run_until_complete(_check()) == []


def test_a_proposal_with_nothing_to_run_is_refused(client, monkeypatch):
    out = _propose(client[1], monkeypatch, prompt="   ")
    assert out.startswith("[propose_schedule error]")
