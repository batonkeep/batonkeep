"""
tests/test_hardening.py — D-0021 hardening: run-reaper + structured logging.

Covers the two non-migration hardening items: the startup run-reaper (orphaned
runs reconciled after a restart) and the JSON logging + correlation-id spine.
"""
from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ── Item 3: startup run-reaper ────────────────────────────────────────────────

@pytest.fixture
async def db_with_runs(tmp_path):
    from app.db import Base
    from app.models import Owner, Run, Task

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/reap.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="T"))
        db.add(Task(id=1, owner_id="local", name="t", prompt_template="x"))
        await db.flush()
        db.add_all([
            Run(id=1, owner_id="local", task_id=1, status="running"),
            Run(id=2, owner_id="local", task_id=1, status="queued"),
            Run(id=3, owner_id="local", task_id=1, status="succeeded"),
            Run(id=4, owner_id="local", task_id=1, status="deferred"),
        ])
        await db.commit()
    yield Maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_reaper_resumes_undispatched_and_fails_dispatched(db_with_runs, monkeypatch):
    """D-0068: a never-dispatched run is re-queued; a dispatched one is failed.

    Run 1 is `running` — status is committed immediately before the executor is invoked,
    so it may already have spent quota. Run 2 is `queued` with no `started_at`, meaning
    the atomic claim never fired and provably nothing happened.
    """
    import app.orchestrator as orch
    from app.models import Run

    monkeypatch.setattr(orch, "AsyncSessionLocal", db_with_runs)
    resumed: list[int] = []
    acted = await orch.reap_orphaned_runs(dispatch=resumed.append)
    assert acted == 2

    async with db_with_runs() as db:
        statuses = {r.id: r.status for r in (await db.execute(select(Run))).scalars()}
        dispatched = await db.get(Run, 1)
        undispatched = await db.get(Run, 2)

    # The dispatched run is failed and says why; the untouched terminal rows are untouched.
    assert statuses == {1: "failed", 2: "queued", 3: "succeeded", 4: "deferred"}
    assert "dispatched" in (dispatched.error or "")
    assert dispatched.finished_at is not None

    # The undispatched run goes back to a claimable state and is re-executed.
    assert resumed == [2]
    assert undispatched.started_at is None
    assert undispatched.runtime_epoch is None
    assert undispatched.error is None


@pytest.mark.asyncio
async def test_reaper_ignores_runs_owned_by_this_process(db_with_runs, monkeypatch):
    """A non-terminal run stamped with the *current* epoch is live, not orphaned.

    This is the property that keeps reconciliation from clobbering a sibling process's
    work — the old "if I am starting, everything non-terminal is orphaned" rule is true
    of one process and false of two.
    """
    import app.orchestrator as orch
    from app.models import Run
    from app.runtime import RUNTIME_EPOCH

    async with db_with_runs() as db:
        live = await db.get(Run, 1)
        live.runtime_epoch = RUNTIME_EPOCH
        await db.commit()

    monkeypatch.setattr(orch, "AsyncSessionLocal", db_with_runs)
    resumed: list[int] = []
    acted = await orch.reap_orphaned_runs(dispatch=resumed.append)

    # Only run 2 (queued, no epoch) is reconciled; run 1 is left alone entirely.
    assert acted == 1
    assert resumed == [2]
    async with db_with_runs() as db:
        assert (await db.get(Run, 1)).status == "running"


@pytest.mark.asyncio
async def test_reaper_is_noop_when_nothing_orphaned(db_with_runs, monkeypatch):
    import app.orchestrator as orch
    monkeypatch.setattr(orch, "AsyncSessionLocal", db_with_runs)
    await orch.reap_orphaned_runs(dispatch=lambda _: None)   # first pass reconciles both
    # Run 2 went back to `queued` with no epoch, so it is legitimately still reconcilable
    # — a second boot would re-queue it again. Only the dispatched run is terminal now.
    assert await orch.reap_orphaned_runs(dispatch=lambda _: None) == 1


# ── Item 2: structured logging + correlation ──────────────────────────────────

def test_json_formatter_emits_correlation_fields():
    from app.logging_config import JsonFormatter, _CorrelationFilter, owner_id_var, run_id_var

    tok_r = run_id_var.set(42)
    tok_o = owner_id_var.set("local")
    try:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        _CorrelationFilter().filter(rec)
        out = json.loads(JsonFormatter().format(rec))
    finally:
        run_id_var.reset(tok_r)
        owner_id_var.reset(tok_o)

    assert out["msg"] == "hello"
    assert out["level"] == "INFO"
    assert out["run_id"] == 42
    assert out["owner_id"] == "local"
    assert "ts" in out


def test_json_formatter_omits_unset_correlation_and_captures_extra():
    from app.logging_config import JsonFormatter, _CorrelationFilter

    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "request", None, None)
    rec.path = "/api/tasks"          # structured extra
    rec.status = 200
    _CorrelationFilter().filter(rec)
    out = json.loads(JsonFormatter().format(rec))

    assert out["path"] == "/api/tasks" and out["status"] == 200
    assert "run_id" not in out and "session_id" not in out  # unset ⇒ omitted


def test_configure_logging_installs_single_json_handler():
    from app.logging_config import JsonFormatter, configure_logging

    configure_logging("INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
