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
async def test_reaper_fails_running_and_queued_only(db_with_runs, monkeypatch):
    import app.orchestrator as orch
    from app.models import Run

    monkeypatch.setattr(orch, "AsyncSessionLocal", db_with_runs)
    reaped = await orch.reap_orphaned_runs()
    assert reaped == 2

    async with db_with_runs() as db:
        statuses = {r.id: r.status for r in (await db.execute(select(Run))).scalars()}
        running = await db.get(Run, 1)
    assert statuses == {1: "failed", 2: "failed", 3: "succeeded", 4: "deferred"}
    assert "restart" in (running.error or "")
    assert running.finished_at is not None


@pytest.mark.asyncio
async def test_reaper_is_noop_when_nothing_orphaned(db_with_runs, monkeypatch):
    import app.orchestrator as orch
    monkeypatch.setattr(orch, "AsyncSessionLocal", db_with_runs)
    await orch.reap_orphaned_runs()              # first pass reaps the 2 orphans
    assert await orch.reap_orphaned_runs() == 0  # second pass: nothing left


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
