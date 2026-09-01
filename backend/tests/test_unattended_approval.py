"""P-0098 / Gate B1–B2 — an unattended run can park on a durable approval.

Before this, `confirmation` on a background run meant the tool was never offered, so the
only options were withhold the capability or run it unsupervised. These tests cover the
third: *ask, and wait* — with the request persisted as a durable adjudication record and
the decision arriving through the general approvals API rather than a session route.

The restart limit is asserted too, deliberately: the *record* outlives the process, the
*parked run* does not. Encoding that stops it being mistaken for full cross-restart
resumption, which needs a checkpointable agent loop.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def maker(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/approve.db")

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


async def _seed_run(Maker) -> int:
    from app.models import Run, Task

    async with Maker() as db:
        task = Task(owner_id="local", name="t", prompt_template="p",
                    exec_policy="confirmation")
        db.add(task)
        await db.commit()
        run = Run(owner_id="local", task_id=task.id, status="running", provider="mock")
        db.add(run)
        await db.commit()
        return run.id


def test_unattended_run_parks_and_proceeds_on_approval(maker, monkeypatch):
    """The whole point: a background run asks, waits, and continues on a verdict."""
    import app.orchestrator as orch
    from app.models import Approval

    async def _body():
        run_id = await _seed_run(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)

        approve = orch._make_run_approver(run_id, "local", "mock")
        task = asyncio.create_task(approve("print('hi')", "run a probe"))

        # Let the approver persist its record and start waiting.
        for _ in range(50):
            await asyncio.sleep(0.01)
            async with maker() as db:
                rows = (await db.execute(
                    __import__("sqlalchemy").select(Approval)
                )).scalars().all()
            if rows:
                break
        assert rows, "the approver must persist a durable record before it waits"
        row = rows[0]
        assert row.status == "pending"
        assert row.kind == "code_exec"
        assert row.run_id == run_id, "the record must bind to the run, not a session"
        assert row.payload["unattended"] is True
        assert not task.done(), "the run must still be parked, not resolved"

        # A verdict arrives later, from wherever.
        assert orch.approvals.resolve(row.request_id, True) is True
        assert await asyncio.wait_for(task, timeout=5) is True

        async with maker() as db:
            settled = await db.get(Approval, row.id)
        assert settled.status == "approved"
        assert settled.decided_by == "human"

    asyncio.get_event_loop().run_until_complete(_body())


def test_denial_is_recorded_and_returned(maker, monkeypatch):
    import app.orchestrator as orch
    from app.models import Approval

    async def _body():
        run_id = await _seed_run(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)

        approve = orch._make_run_approver(run_id, "local", "mock")
        task = asyncio.create_task(approve("import os; os.system('rm -rf /')", None))
        for _ in range(50):
            await asyncio.sleep(0.01)
            async with maker() as db:
                rows = (await db.execute(
                    __import__("sqlalchemy").select(Approval)
                )).scalars().all()
            if rows:
                break

        assert orch.approvals.resolve(rows[0].request_id, False) is True
        assert await asyncio.wait_for(task, timeout=5) is False

        async with maker() as db:
            settled = await db.get(Approval, rows[0].id)
        assert settled.status == "denied"

    asyncio.get_event_loop().run_until_complete(_body())


def test_timeout_is_a_denial_not_an_execution(maker, monkeypatch):
    """Nobody answers. The safe outcome is 'no', never 'go ahead anyway'."""
    import app.orchestrator as orch
    from app.models import Approval

    async def _body():
        run_id = await _seed_run(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        monkeypatch.setattr(orch._settings, "unattended_approval_timeout_seconds", 1)

        approve = orch._make_run_approver(run_id, "local", "mock")
        assert await approve("print(1)", None) is False

        async with maker() as db:
            rows = (await db.execute(
                __import__("sqlalchemy").select(Approval)
            )).scalars().all()
        assert rows[0].status == "denied"
        assert rows[0].decided_by == "timeout", (
            "a timeout must be distinguishable from a human denial in the record"
        )

    asyncio.get_event_loop().run_until_complete(_body())


def test_record_outlives_the_process_but_the_parked_run_does_not(maker, monkeypatch):
    """The honest limit, encoded so it cannot be mistaken for restart survival.

    `reap_pending` expires an unattended run's pending approval on boot: its Future died
    with the process, so nothing is left to release and leaving it `pending` would show
    the operator a decision that could never take effect. Gate A separately fails the run.
    """
    import app.approvals as approvals_mod
    import app.orchestrator as orch
    from app.models import Approval

    async def _body():
        run_id = await _seed_run(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        monkeypatch.setattr(approvals_mod, "AsyncSessionLocal", maker, raising=False)

        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="abc123", kind="code_exec",
                payload={"v": 1, "code": "print(1)", "unattended": True},
                producer="mock", run_id=run_id,
            )
            await db.commit()

        # Simulate the restart: no Future exists for this id in the new process.
        import app.db as db_mod
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", maker)
        reaped = await approvals_mod.reap_pending()
        assert reaped == 1

        async with maker() as db:
            row = (await db.execute(
                __import__("sqlalchemy").select(Approval)
            )).scalars().all()[0]
        assert row.status == "expired", (
            "a pending approval whose run died must not stay decidable"
        )

    asyncio.get_event_loop().run_until_complete(_body())
