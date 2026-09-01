"""P-0106 — a parked run survives a restart and resumes from its conversation.

This is the clause Gate B could not meet (#202): the record outlived the process, the run
did not. The tests here assert the behaviour end to end at the seams that matter —
the approver parking instead of waiting, reconciliation leaving a parked run alone, the
reaper sparing its still-decidable approval, and resume refusing on a fence mismatch.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select


@pytest.fixture
def maker(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/park.db")

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


async def _seed(Maker, *, status="running"):
    from app.models import Run, Task

    async with Maker() as db:
        task = Task(owner_id="local", name="t", prompt_template="p",
                    exec_policy="confirmation")
        db.add(task)
        await db.commit()
        run = Run(owner_id="local", task_id=task.id, status=status,
                  provider="claude-api", model="claude-x")
        db.add(run)
        await db.commit()
        return run.id


def test_approver_parks_instead_of_waiting_when_it_can_checkpoint(maker, monkeypatch):
    """The core behaviour change: raise, don't await."""
    import app.orchestrator as orch
    from app import checkpoint
    from app.models import Approval

    async def _body():
        run_id = await _seed(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        approve = orch._make_run_approver(run_id, "local", "claude-api")

        def _builder():
            return checkpoint.build(
                path="anthropic", provider="claude-api", model="claude-x",
                messages=[{"role": "user", "content": "hi"}],
                usage={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
                round_num=0, tool_call={"id": "t1", "name": "code_exec", "args": "{}"},
            )

        with pytest.raises(checkpoint.ParkRequested):
            await approve("print(1)", "probe", checkpoint=_builder)

        async with maker() as db:
            row = (await db.execute(select(Approval))).scalars().one()
        assert row.status == "pending", "the decision has not been made yet"
        assert row.checkpoint is not None, "parking without storing state would lose the run"
        assert row.checkpoint["tool_call"]["id"] == "t1"

    asyncio.get_event_loop().run_until_complete(_body())


def test_falls_back_to_waiting_when_the_conversation_cannot_be_stored(maker, monkeypatch):
    """Parking must only ever be an improvement over waiting, never a new failure mode."""
    import app.orchestrator as orch
    from app.models import Approval

    async def _body():
        run_id = await _seed(maker)
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        monkeypatch.setattr(orch._settings, "unattended_approval_timeout_seconds", 1)
        approve = orch._make_run_approver(run_id, "local", "claude-api")

        def _broken():
            raise TypeError("cannot serialize <object> into a checkpoint")

        # No ParkRequested: it degrades to the pre-P-0106 in-process wait, which then
        # times out and denies — the old behaviour, intact.
        assert await approve("print(1)", None, checkpoint=_broken) is False

        async with maker() as db:
            row = (await db.execute(select(Approval))).scalars().one()
        assert row.checkpoint is None
        assert row.status == "denied" and row.decided_by == "timeout"

    asyncio.get_event_loop().run_until_complete(_body())


def test_reconciliation_leaves_a_parked_run_alone(maker, monkeypatch):
    """A parked run is non-terminal but NOT an orphan — reconciling it would destroy
    exactly the state P-0106 stores."""
    import app.orchestrator as orch
    from app.models import Run

    async def _body():
        run_id = await _seed(maker, status="parked")
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        acted = await orch.reap_orphaned_runs(dispatch=lambda _: None)
        assert acted == 0
        async with maker() as db:
            assert (await db.get(Run, run_id)).status == "parked"

    asyncio.get_event_loop().run_until_complete(_body())


def test_reaper_spares_a_checkpointed_approval_but_expires_a_waiting_one(maker, monkeypatch):
    """`checkpoint IS NULL` is the distinction: a stored conversation stays decidable
    across a restart; an in-process await does not."""
    import app.approvals as approvals_mod
    import app.db as db_mod
    from app.models import Approval

    async def _body():
        run_id = await _seed(maker, status="parked")
        async with maker() as db:
            parked = await approvals_mod.record_request(
                db, owner_id="local", request_id="parked1", kind="code_exec",
                payload={"v": 1}, producer="claude-api", run_id=run_id,
            )
            parked.checkpoint = {"v": 1, "fence": {}, "messages": []}
            await approvals_mod.record_request(
                db, owner_id="local", request_id="waiting1", kind="code_exec",
                payload={"v": 1}, producer="claude-api", run_id=run_id,
            )
            await db.commit()

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", maker)
        assert await approvals_mod.reap_pending() == 1

        async with maker() as db:
            rows = {r.request_id: r.status
                    for r in (await db.execute(select(Approval))).scalars()}
        assert rows == {"parked1": "pending", "waiting1": "expired"}

    asyncio.get_event_loop().run_until_complete(_body())


def test_resume_refuses_on_a_fence_mismatch_and_fails_the_run_with_the_reason(maker, monkeypatch):
    """D-0069's safety valve: refuse honestly rather than replay stale provider state."""
    import app.orchestrator as orch
    from app import approvals as approvals_mod
    from app.models import Run

    async def _body():
        run_id = await _seed(maker, status="parked")
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        async with maker() as db:
            row = await approvals_mod.record_request(
                db, owner_id="local", request_id="p1", kind="code_exec",
                payload={"v": 1}, producer="claude-api", run_id=run_id,
            )
            row.checkpoint = {
                "v": 1,
                "fence": {"path": "anthropic", "provider": "claude-api",
                          "model": "claude-x", "sdk": "0.0.1-ancient"},
                "messages": [], "usage": {}, "round": 0,
                "tool_call": {"id": "t1", "name": "code_exec", "args": "{}"},
            }
            await db.commit()

        why = await orch.resume_parked_run(run_id, approved=True)
        assert why and "SDK" in why

        async with maker() as db:
            run = await db.get(Run, run_id)
        assert run.status == "failed"
        assert "cannot resume after approval" in (run.error or "")

    asyncio.get_event_loop().run_until_complete(_body())


def test_resume_requeues_a_parked_run_and_hands_over_the_verdict(maker, monkeypatch):
    """The happy path: the run goes back to a claimable state carrying its conversation."""
    import app.orchestrator as orch
    from app import approvals as approvals_mod
    from app import checkpoint
    from app.models import Run

    async def _body():
        run_id = await _seed(maker, status="parked")
        monkeypatch.setattr(orch, "AsyncSessionLocal", maker)
        spawned: list[int] = []

        # Replace the run driver, NOT asyncio.create_task: `orch.asyncio` *is* the asyncio
        # module, so patching create_task there breaks the loop's own internals and hangs
        # the suite. Patching the target coroutine keeps the real dispatch path intact.
        async def _fake_execute(rid: int):
            spawned.append(rid)

        monkeypatch.setattr(orch, "execute_run", _fake_execute)

        cp = checkpoint.build(
            path="anthropic", provider="claude-api", model="claude-x",
            messages=[{"role": "user", "content": "hi"}],
            usage={"tokens_in": 5, "tokens_out": 6, "cost_usd": 0.25},
            round_num=1, tool_call={"id": "t1", "name": "code_exec", "args": "{}"},
        )
        async with maker() as db:
            row = await approvals_mod.record_request(
                db, owner_id="local", request_id="p1", kind="code_exec",
                payload={"v": 1}, producer="claude-api", run_id=run_id,
            )
            row.checkpoint = cp
            await db.commit()

        assert await orch.resume_parked_run(run_id, approved=True) is None
        await asyncio.sleep(0)  # let the dispatched task actually start
        assert spawned == [run_id]

        async with maker() as db:
            run = await db.get(Run, run_id)
        assert run.status == "queued", "must re-enter through the atomic claim"
        assert run.runtime_epoch is None, "an unowned run must carry no ownership stamp"

        ctx = orch._RESUME_CONTEXT[run_id]
        assert ctx["approved"] is True
        assert ctx["checkpoint"]["usage"]["cost_usd"] == 0.25, (
            "spend must carry over, or a resumed run gets a fresh budget for free"
        )

    asyncio.get_event_loop().run_until_complete(_body())
