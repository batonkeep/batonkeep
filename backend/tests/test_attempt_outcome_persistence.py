"""P-0105 — per-attempt outcomes must survive the round trip to the database.

The bug this guards against was invisible to any assertion made against the in-memory
``Run``: the attempt dicts are finalised by in-place mutation, so the object always looked
right while the column never moved. Every assertion here therefore **re-reads the row in a
fresh session** after commit. Asserting on ``run.attempts`` without leaving the session
would pass against the broken code and prove nothing.

Style note: these are sync tests driving async work through one
``run_until_complete`` loop, per the house pattern described in ``conftest.py`` — it keeps
the SQLAlchemy async engine bound to a single loop for setup, body and dispose.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def maker(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/p0105.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # expire_on_commit=False so reading an attribute after commit does not trigger
        # a lazy refresh; each assertion below opens its own session deliberately.
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
        task = Task(owner_id="local", name="t", prompt_template="p")
        db.add(task)
        await db.commit()
        run = Run(owner_id="local", task_id=task.id, status="running")
        db.add(run)
        await db.commit()
        return run.id


def test_terminal_outcome_reaches_the_database(maker):
    """The exact lifecycle that was broken: append 'pending', dispatch, mutate, persist."""
    from app.models import Run
    from app.orchestrator import record_attempts

    async def _body():
        run_id = await _seed_run(maker)

        async with maker() as db:
            run = await db.get(Run, run_id)

            # 1. pre-dispatch marker — this write always worked (the list is new)
            attempt = {"provider": "agy", "outcome": "pending"}
            attempts = [attempt]
            record_attempts(run, attempts)
            await db.commit()

            # 2. the stream ends and the attempt is finalised IN PLACE — the step whose
            #    result never used to reach the column.
            attempt["outcome"] = "success"
            record_attempts(run, attempts)
            await db.commit()

        # 3. fresh session: what does the row actually say?
        async with maker() as db2:
            reloaded = await db2.get(Run, run_id)
            assert reloaded.attempts == [{"provider": "agy", "outcome": "success"}], (
                "terminal outcome did not survive the round trip — the JSON column was "
                "not flagged dirty after in-place mutation (P-0105)"
            )

    asyncio.get_event_loop().run_until_complete(_body())


def test_failover_records_distinct_outcomes_per_attempt(maker):
    """Failover analysis is the point of this column: each attempt keeps its own outcome."""
    from app.models import Run
    from app.orchestrator import record_attempts

    async def _body():
        run_id = await _seed_run(maker)

        async with maker() as db:
            run = await db.get(Run, run_id)
            first = {"provider": "claude", "outcome": "pending"}
            attempts = [first]
            record_attempts(run, attempts)
            await db.commit()

            first["outcome"] = "rate_limited"
            second = {"provider": "agy", "outcome": "pending"}
            attempts.append(second)
            record_attempts(run, attempts)
            await db.commit()

            second["outcome"] = "success"
            record_attempts(run, attempts)
            await db.commit()

        async with maker() as db2:
            reloaded = await db2.get(Run, run_id)
            assert reloaded.attempts == [
                {"provider": "claude", "outcome": "rate_limited"},
                {"provider": "agy", "outcome": "success"},
            ], "per-attempt outcomes must be persisted independently for failover analysis"

    asyncio.get_event_loop().run_until_complete(_body())


def test_stored_value_does_not_alias_the_working_list(maker):
    """The ORM-held value must be a copy, so later mutation cannot rewrite history.

    Without the copy, mutating an attempt after persisting it would silently change what
    the session believes is already stored — which is how "persisted" state drifts from
    the row inside a single transaction.
    """
    from app.models import Run
    from app.orchestrator import record_attempts

    async def _body():
        run_id = await _seed_run(maker)

        async with maker() as db:
            run = await db.get(Run, run_id)
            attempt = {"provider": "agy", "outcome": "pending"}
            record_attempts(run, [attempt])
            await db.commit()

            # Mutating the working copy must NOT retroactively alter the stored value.
            attempt["outcome"] = "error"
            assert run.attempts == [{"provider": "agy", "outcome": "pending"}], (
                "Run.attempts aliases the caller's dicts — the stored value changed "
                "without an explicit persist"
            )

    asyncio.get_event_loop().run_until_complete(_body())
