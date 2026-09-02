"""A cancelled run must not strand its pending approval.

Found by probing whether the stale row on the testbed was a one-off. It is not: cancelling
a run that is waiting on an approval sets the run terminal and leaves the approval
`pending` forever. The inbox then offers a decision that cannot succeed — which is the
fastest way to teach an operator to stop trusting the queue.
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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/strand.db")

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


async def _seed(Maker, status="running"):
    from app.models import Run, Task

    async with Maker() as db:
        task = Task(owner_id="local", name="t", prompt_template="p")
        db.add(task)
        await db.commit()
        run = Run(owner_id="local", task_id=task.id, status=status, provider="mock")
        db.add(run)
        await db.commit()
        return run.id


def test_cancelling_a_run_settles_its_pending_approval(maker, monkeypatch):
    """The live path the probe was looking for."""
    import app.approvals as approvals_mod
    import app.orchestrator as orch
    from app.models import Approval, Run

    async def _body():
        run_id = await _seed(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="q1", kind="code_exec",
                payload={"v": 1}, producer="mock", run_id=run_id,
            )
            await db.commit()

        # A live in-flight task, as a run waiting in-process on an approval has.
        async def _forever():
            await asyncio.sleep(60)

        handle = asyncio.create_task(_forever())
        orch._cancel_handles[run_id] = handle
        try:
            assert await orch.cancel_run(run_id) is True
        finally:
            handle.cancel()
            orch._cancel_handles.pop(run_id, None)

        async with maker() as db:
            run = await db.get(Run, run_id)
            appr = (await db.execute(select(Approval))).scalars().one()
        assert run.status == "cancelled"
        assert appr.status != "pending", (
            "a cancelled run must not leave a decision the operator can never act on"
        )
        assert appr.decided_by == "cancelled"

    asyncio.get_event_loop().run_until_complete(_body())


def test_an_approval_on_a_terminal_run_is_not_offered_as_resumable(maker):
    """Defence in depth, for rows stranded before the fix (or by any path not yet found).

    `resumable` answers "was a checkpoint stored" — for a run that has since terminated
    that is true and useless. The queue must answer "can this still be acted on".
    """
    import app.main as main
    from app.db import get_db
    from app.models import Approval

    async def _body():
        run_id = await _seed(maker, status="succeeded")
        async with maker() as db:
            db.add(Approval(owner_id="local", request_id="q2", kind="code_exec",
                            status="pending", run_id=run_id, producer="mock",
                            checkpoint={"v": 1, "fence": {}, "messages": []}))
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_body())

    async def _get_db():
        async with maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        rows = TestClient(main.app).get("/api/approvals").json()
    finally:
        main.app.dependency_overrides.pop(get_db, None)

    row = next(r for r in rows if r["request_id"] == "q2")
    assert row["status"] == "pending", (
        "it stays visible — an undecided approval is audit-relevant"
    )
    assert row["resumable"] is False, "but it must not be offered as actionable"
