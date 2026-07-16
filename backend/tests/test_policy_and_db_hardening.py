"""
tests/test_policy_and_db_hardening.py — D-0058 slice 1.

Two concerns:
  • `resolve_effective_policy()` (app/policy.py, seam 1) — the single entrypoint
    execution paths use for declared constraints; asserts it composes the
    existing Task/Session fields with unchanged semantics.
  • SQLite write-hardening (A1) — the engine's per-connection pragmas
    (busy_timeout + journal mode) and the journal-mode chooser's override /
    network-fs behavior.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models import Session, Task
from app.policy import resolve_effective_policy


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    yield
    from app.config import get_settings

    get_settings.cache_clear()
    get_settings()


# ── resolve_effective_policy ──────────────────────────────────────────────────

def test_task_policy_composes_declared_fields():
    task = Task(
        name="t", prompt_template="p", exec_policy="allow-safe",
        routing={"confidential": True}, timeout_seconds=120,
    )
    p = resolve_effective_policy(task=task)
    assert p.exec_policy == "allow-safe"
    assert p.confidential is True
    assert p.timeout_seconds == 120
    assert p.budget_cap_usd is None  # task lane: per-run budget is a lane default


def test_task_policy_defaults_fall_through():
    from app.config import get_settings

    task = Task(name="t", prompt_template="p", exec_policy="confirmation",
                routing=None, timeout_seconds=None)
    p = resolve_effective_policy(task=task)
    assert p.confidential is False
    assert p.timeout_seconds == get_settings().run_timeout_seconds


def test_session_policy_composes_declared_fields():
    session = Session(
        title="s", workspace_path="/tmp/x", exec_policy="auto",
        confidential=True, budget_usd=2.5,
    )
    p = resolve_effective_policy(session=session)
    assert p.exec_policy == "auto"
    assert p.confidential is True
    assert p.budget_cap_usd == 2.5


def test_no_object_yields_deployment_defaults():
    p = resolve_effective_policy()
    assert p.exec_policy == "confirmation"
    assert p.confidential is False


# ── SQLite write-hardening (A1) ───────────────────────────────────────────────

def test_journal_mode_explicit_override(monkeypatch):
    from app.config import get_settings
    from app.db import _sqlite_journal_mode

    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "truncate")
    get_settings.cache_clear()
    assert _sqlite_journal_mode("/data/batonkeep.db") == "truncate"


def test_journal_mode_defaults_to_wal_locally(monkeypatch):
    from app.config import get_settings
    from app.db import _sqlite_journal_mode

    monkeypatch.delenv("SQLITE_JOURNAL_MODE", raising=False)
    get_settings.cache_clear()
    # On any local filesystem (or where /proc/mounts is absent — macOS CI),
    # detection must land on WAL.
    assert _sqlite_journal_mode("/tmp/does-not-matter.db") == "wal"


@pytest.mark.asyncio
async def test_engine_applies_pragmas(tmp_path, monkeypatch):
    """A fresh engine's connections carry busy_timeout + WAL."""
    from app.config import get_settings
    from app.db import _SQLITE_BUSY_TIMEOUT_MS, _make_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/pragmas.db")
    get_settings.cache_clear()
    eng = _make_engine()
    try:
        async with eng.connect() as conn:
            timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        assert timeout == _SQLITE_BUSY_TIMEOUT_MS
        assert str(journal).lower() == "wal"
    finally:
        await eng.dispose()
