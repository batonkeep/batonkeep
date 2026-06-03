"""
tests/test_p7.py — P7 gate: scheduler, stats, credentials, seed.

Tests:
- seed_if_empty inserts the §14 seeds exactly once (idempotent)
- /stats aggregates runs_today, success_rate, runs_by_provider, failover_rate, deferred_now
- credentials encrypt/decrypt roundtrip + delete
- scheduler registers interval/cron jobs and skips invalid exprs
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Credential, Owner, Run, Task


# ── Fresh DB fixture (mirrors test_orchestrator.fresh_db) ─────────────────────

@pytest.fixture
async def fresh_db(tmp_path):
    from app.db import Base
    from app.models import Owner, Task, Run, RunEvent, Credential  # register metadata

    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add(Owner(id="local", label="Test"))
        await db.commit()

    yield engine, Session, tmp_path
    await engine.dispose()


# ── Seed ──────────────────────────────────────────────────────────────────────

class TestSeed:
    @pytest.mark.asyncio
    async def test_seed_inserts_once_and_is_idempotent(self, fresh_db):
        engine, Session, _ = fresh_db
        import app.seed as seed_mod
        from app.seed import SEED_TASKS

        orig = seed_mod.AsyncSessionLocal
        seed_mod.AsyncSessionLocal = Session
        try:
            await seed_mod.seed_if_empty("local")
            await seed_mod.seed_if_empty("local")  # second call must be a no-op

            from sqlalchemy import select, func
            async with Session() as db:
                count = await db.scalar(select(func.count(Task.id)))
            assert count == len(SEED_TASKS)
        finally:
            seed_mod.AsyncSessionLocal = orig


# ── Stats ───────────────────────────────────────────────────────────────────--

class TestStats:
    @pytest.mark.asyncio
    async def test_stats_aggregates(self, fresh_db):
        engine, Session, _ = fresh_db
        from app.main import get_stats

        now = datetime.now(timezone.utc)
        async with Session() as db:
            task = Task(owner_id="local", name="T", prompt_template="x")
            db.add(task)
            await db.commit()
            await db.refresh(task)

            # 2 succeeded (one needed failover), 1 failed, 1 deferred
            db.add(Run(owner_id="local", task_id=task.id, status="succeeded",
                       provider="mock", cost_usd=0.5, started_at=now,
                       finished_at=now + timedelta(seconds=2),
                       attempts=[{"provider": "mock", "outcome": "success"}]))
            db.add(Run(owner_id="local", task_id=task.id, status="succeeded",
                       provider="claude", cost_usd=1.0, started_at=now,
                       finished_at=now + timedelta(seconds=4),
                       attempts=[{"provider": "grok", "outcome": "rate_limited"},
                                 {"provider": "claude", "outcome": "success"}]))
            db.add(Run(owner_id="local", task_id=task.id, status="failed", provider="mock"))
            db.add(Run(owner_id="local", task_id=task.id, status="deferred",
                       deferred_until=now + timedelta(hours=1)))
            await db.commit()

            stats = await get_stats(db=db, owner_id="local")

        assert stats.runs_today == 4
        assert stats.success_rate == pytest.approx(2 / 3, abs=1e-3)  # 2 ok of 3 terminal
        assert stats.runs_by_provider == {"mock": 2, "claude": 1}
        assert stats.failover_rate == pytest.approx(1 / 3, abs=1e-3)  # 1 of 3 terminal
        assert stats.deferred_now == 1
        assert stats.cost_today_usd == pytest.approx(1.5)
        assert stats.avg_duration_ms == pytest.approx(3000.0)  # (2000 + 4000) / 2


# ── Credentials ─────────────────────────────────────────────────────────────--

class TestCredentials:
    @pytest.mark.asyncio
    async def test_encrypt_roundtrip_and_delete(self, fresh_db):
        engine, Session, _ = fresh_db
        from app.config import get_settings
        from app.credentials import (
            store_credential, get_credential, delete_credential, list_credentials,
        )
        # Force a non-empty secret so Fernet encryption path is exercised.
        # Settings is a frozen pydantic model — set via __dict__ (as in test_orchestrator).
        settings = get_settings()
        orig_secret = settings.app_secret
        settings.__dict__["app_secret"] = "test-secret-key"
        try:
            async with Session() as db:
                await store_credential(db, "local", "openai", "sk-secret-123")

                # Ciphertext on disk must not equal the plaintext (when crypto available)
                from sqlalchemy import select
                cred = (await db.execute(select(Credential))).scalar_one()
                try:
                    from cryptography.fernet import Fernet  # noqa: F401
                    assert cred.ciphertext != "sk-secret-123"
                except ImportError:
                    pass  # plaintext dev fallback

                assert await get_credential(db, "local", "openai") == "sk-secret-123"
                assert [c["provider"] for c in await list_credentials(db, "local")] == ["openai"]

                assert await delete_credential(db, "local", "openai") is True
                assert await get_credential(db, "local", "openai") is None
        finally:
            settings.__dict__["app_secret"] = orig_secret


# ── Scheduler ─────────────────────────────────────────────────────────────────

class TestScheduler:
    def _task(self, **kw):
        t = Task(name="T", prompt_template="x")
        t.id = kw.pop("id", 1)
        t.enabled = kw.pop("enabled", True)
        t.schedule_kind = kw.pop("schedule_kind", "none")
        t.schedule_expr = kw.pop("schedule_expr", None)
        return t

    def test_interval_task_registers_job(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from app.scheduler import _sync_task

        sched = AsyncIOScheduler(timezone="UTC")
        _sync_task(sched, self._task(id=10, schedule_kind="interval", schedule_expr="60"))
        assert sched.get_job("task_10") is not None

    def test_cron_task_registers_job(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from app.scheduler import _sync_task

        sched = AsyncIOScheduler(timezone="UTC")
        _sync_task(sched, self._task(id=11, schedule_kind="cron", schedule_expr="0 7 * * *"))
        assert sched.get_job("task_11") is not None

    def test_none_and_invalid_register_no_job(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from app.scheduler import _sync_task

        sched = AsyncIOScheduler(timezone="UTC")
        _sync_task(sched, self._task(id=12, schedule_kind="none"))
        _sync_task(sched, self._task(id=13, schedule_kind="cron", schedule_expr="not-a-cron"))
        _sync_task(sched, self._task(id=14, schedule_kind="interval", schedule_expr="abc"))
        assert sched.get_job("task_12") is None
        assert sched.get_job("task_13") is None
        assert sched.get_job("task_14") is None

    def test_disabled_task_removes_job(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from app.scheduler import _sync_task

        sched = AsyncIOScheduler(timezone="UTC")
        _sync_task(sched, self._task(id=15, schedule_kind="interval", schedule_expr="60"))
        assert sched.get_job("task_15") is not None
        _sync_task(sched, self._task(id=15, enabled=False, schedule_kind="interval", schedule_expr="60"))
        assert sched.get_job("task_15") is None
