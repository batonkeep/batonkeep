"""Reproduction: does a single run execute the agent more than once?"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.providers.base import EventKind, ExecEvent, ExecResult, Executor, Usage


class CountingExecutor(Executor):
    """Counts run_stream invocations. Optionally omits the terminal result event."""

    calls = 0  # class-level so all instances share the tally

    def __init__(self, name: str = "mock", *, emit_result: bool = True) -> None:
        self.name = name
        self.tier = "mock"
        self._emit_result = emit_result

    @property
    def kind(self) -> str:
        return "mock"

    def is_healthy(self) -> bool:
        return True

    async def run_stream(self, prompt: str, *, workdir: str, **kw) -> AsyncIterator[ExecEvent]:
        CountingExecutor.calls += 1
        yield ExecEvent(kind=EventKind.phase, phase="running")
        yield ExecEvent(kind=EventKind.token, text="# Report\n```json\n{\"x\":1}\n```\n")
        if self._emit_result:
            usage = Usage(tokens_in=10, tokens_out=20, cost_usd=0.0)
            result = ExecResult(text="# Report\n```json\n{\"x\":1}\n```\n",
                                usage=usage, provider=self.name, model="m")
            yield ExecEvent(kind=EventKind.result, message="done",
                            data={"result": result, "usage": usage.__dict__})
        # else: stream just ends with no terminal event


@pytest.fixture
async def fresh_db(tmp_path):
    from app.db import Base
    from app.models import Owner  # noqa: F401  register metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    from app.models import Owner
    async with Session() as db:
        db.add(Owner(id="local", label="T"))
        await db.commit()
    yield engine, Session, tmp_path
    await engine.dispose()


async def _run_task(Session, tmp_path, routing, emit_result):
    import app.orchestrator as orch
    from app.models import Run, Task

    CountingExecutor.calls = 0
    orig_sl = orch.AsyncSessionLocal
    orig_get = orch.get_executor
    orch.AsyncSessionLocal = Session
    orch.get_executor = lambda iid: CountingExecutor(iid, emit_result=emit_result)
    saved = {k: getattr(orch._settings, k)
             for k in ("outputs_dir", "work_dir", "retry_backoff_seconds")}
    orch._settings.outputs_dir = str(tmp_path / "out")
    orch._settings.work_dir = str(tmp_path / "work")
    orch._settings.retry_backoff_seconds = 0
    try:
        async with Session() as db:
            task = Task(owner_id="local", name="Frontier LLM Comparison",
                        prompt_template="compare", params={}, routing=routing,
                        want_markdown=True, want_json=True)
            db.add(task)
            await db.commit()
            await db.refresh(task)
            tid = task.id
        run = await orch.enqueue_run(tid, trigger="test")
        bg = orch._cancel_handles.get(run.id)
        if bg:
            try:
                await asyncio.wait_for(asyncio.shield(bg), timeout=10)
            except asyncio.TimeoutError:
                pass
        async with Session() as db:
            run = await db.get(Run, run.id)
            n_runs = await db.scalar(select(func.count(Run.id)))
        return run, n_runs, CountingExecutor.calls
    finally:
        orch.AsyncSessionLocal = orig_sl
        orch.get_executor = orig_get
        # Restore VALUES — never pop/delete fields off the shared pydantic
        # instance (a deleted field breaks every later reader/patcher).
        for k, v in saved.items():
            setattr(orch._settings, k, v)


@pytest.mark.asyncio
async def test_single_candidate_success_runs_once(fresh_db):
    _, Session, tmp_path = fresh_db
    routing = {"strategy": "capability", "candidates": ["mock"], "failover": True, "max_attempts": 3}
    run, n_runs, calls = await _run_task(Session, tmp_path, routing, emit_result=True)
    print(f"\n[emit_result=True] status={run.status} run_rows={n_runs} agent_calls={calls}")
    assert run.status == "succeeded"
    assert calls == 1, f"agent executed {calls}× for one run"


@pytest.mark.asyncio
async def test_candidate_no_terminal_result_fails_honestly(fresh_db):
    """A candidate that never emits a terminal result must not silently re-run the
    chain forever; it fails with a clear error and produces exactly one Run row."""
    _, Session, tmp_path = fresh_db
    routing = {"strategy": "capability", "candidates": ["mock"], "failover": False, "max_attempts": 1}
    run, n_runs, calls = await _run_task(Session, tmp_path, routing, emit_result=False)
    print(f"\n[emit_result=False] status={run.status} run_rows={n_runs} agent_calls={calls}")
    assert n_runs == 1, "a single request must never spawn extra Run rows"
    assert run.status in ("failed", "deferred")
    assert run.error and "output" in run.error.lower()
    assert calls == 1, f"agent executed {calls}× for one failed run"
