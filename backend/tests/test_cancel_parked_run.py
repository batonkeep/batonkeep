"""Cancelling a parked run must actually cancel it ([[P-0110]]).

Found by `DRILL-PARK-R5` on Runtime B. `POST /api/runs/{id}/cancel` on a parked run
answered **200 OK with the run still parked** — approval still pending, still offered as
`resumable` in the queue. It did not fail; it reported success and did nothing.

Two bugs in one: the orchestrator had no path for a run with no live task (a parked run
has none *by design* — P-0106 freed the process so the run could outlive it), and the HTTP
layer discarded the boolean that said so. A safety control that reports success while
doing nothing is worse than a missing one, because the operator stops looking.

Deny and cancel are different intentions and both are needed: deny refuses one proposed
action and lets the run carry on to propose the next; cancel stops the run.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture
def maker(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cancel.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            await db.commit()
        return Maker

    loop = asyncio.get_event_loop()
    Maker = loop.run_until_complete(_setup())
    try:
        yield Maker
    finally:
        loop.run_until_complete(engine.dispose())


async def _seed(Maker, status: str):
    from app.models import Run, Task

    async with Maker() as db:
        task = Task(owner_id="local", name="t", prompt_template="p")
        db.add(task)
        await db.commit()
        run = Run(owner_id="local", task_id=task.id, status=status, provider="mock")
        db.add(run)
        await db.commit()
        return run.id


def test_a_parked_run_can_be_cancelled(maker, monkeypatch):
    """The live path the drill exercised: no in-process task, and it must still work."""
    import app.approvals as approvals_mod
    import app.orchestrator as orch
    from app.models import Approval, Run

    async def _body():
        run_id = await _seed(maker, "parked")
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="q1", kind="code_exec",
                payload={"v": 1}, producer="mock", run_id=run_id,
            )
            await db.commit()

        assert orch._cancel_handles.get(run_id) is None, (
            "a parked run has no in-process task — that is P-0106 working, not a problem"
        )
        assert await orch.cancel_run(run_id) is True

        async with maker() as db:
            run = await db.get(Run, run_id)
            appr = (await db.execute(select(Approval))).scalars().one()
        assert run.status == "cancelled"
        assert run.finished_at is not None
        assert appr.status == "expired", "the decision it was waiting on is closed out"
        assert appr.decided_by == "cancelled", "nobody refused it — the work went away"

    asyncio.get_event_loop().run_until_complete(_body())


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "deferred"])
def test_a_run_that_cannot_be_cancelled_says_so(maker, monkeypatch, status):
    import app.orchestrator as orch

    async def _body():
        run_id = await _seed(maker, status)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        assert await orch.cancel_run(run_id) is False

    asyncio.get_event_loop().run_until_complete(_body())


def _client(maker):
    import app.main as main
    from app.db import get_db

    async def _get_db():
        async with maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    return main.app, TestClient(main.app)


def test_the_api_no_longer_answers_200_to_a_cancel_that_did_nothing(maker, monkeypatch):
    """The half that made this dangerous rather than merely missing."""
    import app.main as main
    import app.orchestrator as orch
    from app.db import get_db

    run_id = asyncio.get_event_loop().run_until_complete(_seed(maker, "succeeded"))
    monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
    app, client = _client(maker)
    try:
        r = client.post(f"/api/runs/{run_id}/cancel")
    finally:
        main.app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 409, "a no-op must not be reported as success"
    assert "succeeded" in r.json()["detail"], "say what state it is actually in"


def test_a_deferred_run_is_told_what_to_do_instead(maker, monkeypatch):
    """`deferred` has a real answer (requeue) that the word does not suggest."""
    import app.main as main
    import app.orchestrator as orch
    from app.db import get_db

    run_id = asyncio.get_event_loop().run_until_complete(_seed(maker, "deferred"))
    monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
    app, client = _client(maker)
    try:
        r = client.post(f"/api/runs/{run_id}/cancel")
    finally:
        main.app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 409
    assert "requeue" in r.json()["detail"].lower()


def test_cancelling_a_parked_run_over_http_terminates_it(maker, monkeypatch):
    import app.main as main
    import app.orchestrator as orch
    from app.db import get_db
    from app.models import Run

    run_id = asyncio.get_event_loop().run_until_complete(_seed(maker, "parked"))
    monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
    app, client = _client(maker)
    try:
        r = client.post(f"/api/runs/{run_id}/cancel")
    finally:
        main.app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    async def _check():
        async with maker() as db:
            return (await db.get(Run, run_id)).status

    assert asyncio.get_event_loop().run_until_complete(_check()) == "cancelled"
