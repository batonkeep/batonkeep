"""P-0107 shape (b) / Gate C1 — agents as a grouping over existing nouns.

The properties worth pinning are the judgement calls, not the SQL: that only scheduled
work counts as an agent, that health is measured by whether it *delivered* rather than by
transport status, and that the identity returned is the same one the attribution envelope
uses — so a durable `Agent` entity later inherits it instead of replacing it.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner, Project

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/agents.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="Ops"))
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


def _seed(Maker):
    from app.models import Approval, Run, Task

    async def _go():
        now = datetime.now(UTC)
        async with Maker() as db:
            scheduled = Task(owner_id="local", project_id="p1", name="Daily Brief",
                             prompt_template="x", schedule_expr="0 7 * * *",
                             routing={"candidates": ["agy"]})
            oneshot = Task(owner_id="local", name="Ad hoc", prompt_template="x")
            db.add_all([scheduled, oneshot])
            await db.commit()

            # Delivered.
            db.add(Run(owner_id="local", task_id=scheduled.id, status="succeeded",
                       created_at=now - timedelta(hours=2)))
            # Reported success, produced nothing — the case transport status hides.
            db.add(Run(owner_id="local", task_id=scheduled.id, status="succeeded",
                       output_flags={"outputs_missing": True},
                       created_at=now - timedelta(hours=1)))
            parked = Run(owner_id="local", task_id=scheduled.id, status="parked",
                         created_at=now)
            db.add(parked)
            await db.commit()
            db.add(Approval(owner_id="local", request_id="r1", kind="code_exec",
                            status="pending", run_id=parked.id, producer="agy"))
            await db.commit()
        return scheduled.id

    return asyncio.get_event_loop().run_until_complete(_go())


def test_only_scheduled_work_is_an_agent(client):
    """A one-shot task is a job someone ran, not a thing that persists and acts.
    Calling every task an agent would make the word mean nothing."""
    c, Maker = client
    _seed(Maker)
    agents = c.get("/api/agents").json()
    assert [a["name"] for a in agents] == ["Daily Brief"]


def test_identity_matches_the_attribution_envelope(client):
    """The principal a durable Agent would carry (D-0059 D3), so shape (c) later inherits
    this identity rather than replacing it."""
    c, Maker = client
    task_id = _seed(Maker)
    a = c.get("/api/agents").json()[0]
    assert a["principal_id"] == f"agent:task/{task_id}"


def test_health_is_measured_by_delivery_not_transport(client):
    """An agent that reports success while producing nothing is not healthy. This is the
    P-0070 rule applied to the surface an operator actually judges an agent by."""
    c, Maker = client
    _seed(Maker)
    a = c.get("/api/agents").json()[0]
    assert a["recent_failures"] == 1, "the outputs_missing run must count against it"
    assert a["runs_total"] == 3


def test_a_parked_run_is_shown_as_waiting_not_failing(client):
    """Waiting on a human is not a failure — miscounting it would train the operator to
    ignore the number that matters."""
    c, Maker = client
    _seed(Maker)
    a = c.get("/api/agents").json()[0]
    assert a["awaiting"] == 1
    assert a["last_outcome"] == "parked"


def test_context_an_operator_needs_is_present(client):
    c, Maker = client
    _seed(Maker)
    a = c.get("/api/agents").json()[0]
    assert a["project_name"] == "Ops"
    assert a["schedule_expr"] == "0 7 * * *"
    assert a["provider"] == "agy", "a single pinned candidate is the agent's provider"
    assert a["enabled"] is True
