"""
tests/test_db_migrations.py — schema is managed by alembic (D-0021).

`init_db()` brings the DB to alembic head. This replaced the old SQLite-only
additive-column backfill. These tests cover the three startup states init_db must
handle (fresh / already-managed / legacy-pre-alembic) plus a drift guard that fails
if the models diverge from the latest migration (the regression the old backfill
existed to prevent — a model column with no schema change — now caught at CI).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    """These tests repoint DATABASE_URL + clear the Settings cache; rebuild it
    afterwards so the tmp-DB singleton doesn't leak to the next test (the conftest
    `_coherent_settings` fixture then re-converges every module on it)."""
    yield
    from app.config import get_settings

    get_settings.cache_clear()
    get_settings()


def _use_db(monkeypatch, url_path: str) -> None:
    """Point app settings at a tmp SQLite file and clear the cached Settings."""
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{url_path}")
    get_settings.cache_clear()


def _sync_engine(url_path: str):
    return create_engine(f"sqlite:///{url_path}")


@pytest.mark.asyncio
async def test_fresh_db_upgrade_creates_full_schema(tmp_path, monkeypatch):
    """A fresh DB is brought to head — every table + every column the old backfill added."""
    import app.db as db

    path = f"{tmp_path}/fresh.db"
    _use_db(monkeypatch, path)
    await db.init_db()

    eng = _sync_engine(path)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    # All app tables + the alembic bookkeeping table.
    assert {"owners", "tasks", "runs", "run_events", "sessions",
            "session_turns", "artifacts", "credentials", "alembic_version"} <= tables
    # The columns the legacy backfill used to add must be present from the baseline.
    turn_cols = {c["name"] for c in insp.get_columns("session_turns")}
    assert {"commit_sha", "diffstat", "changed_files"} <= turn_cols
    cred_cols = {c["name"] for c in insp.get_columns("credentials")}
    assert {"label", "key_hint", "last_used_at"} <= cred_cols
    sess_cols = {c["name"] for c in insp.get_columns("sessions")}
    assert {"cf_project", "confidential"} <= sess_cols
    eng.dispose()


@pytest.mark.asyncio
async def test_idempotent_second_run_is_noop(tmp_path, monkeypatch):
    import app.db as db

    path = f"{tmp_path}/idem.db"
    _use_db(monkeypatch, path)
    await db.init_db()
    await db.init_db()  # must not error or duplicate anything

    eng = _sync_engine(path)
    with eng.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version  # stamped at a real revision
    eng.dispose()


@pytest.mark.asyncio
async def test_unknown_newer_revision_refuses_start(tmp_path, monkeypatch):
    """A DB migrated by a newer binary (alembic revision this binary's scripts don't
    know) must fail closed at startup — additive-only evolution covers JSON payload
    readers, not schema semantics, so an older binary must not write a newer schema."""
    import app.db as db

    path = f"{tmp_path}/newer.db"
    _use_db(monkeypatch, path)
    await db.init_db()

    eng = _sync_engine(path)
    with eng.connect() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
        conn.commit()
    eng.dispose()

    with pytest.raises(RuntimeError, match="unknown to this binary"):
        await db.init_db()


@pytest.mark.asyncio
async def test_unknown_newer_revision_override_skips_migrations(tmp_path, monkeypatch):
    """DB_ALLOW_UNKNOWN_REVISION=1 is the explicit escape hatch: startup proceeds,
    migrations are skipped, and the stored (newer) revision is left untouched."""
    import app.db as db

    path = f"{tmp_path}/newer_override.db"
    _use_db(monkeypatch, path)
    await db.init_db()

    eng = _sync_engine(path)
    with eng.connect() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
        conn.commit()
    eng.dispose()

    monkeypatch.setenv("DB_ALLOW_UNKNOWN_REVISION", "1")
    await db.init_db()  # must not raise

    eng = _sync_engine(path)
    with eng.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "ffffffffffff"  # untouched — no downgrade/upgrade attempted
    eng.dispose()


@pytest.mark.asyncio
async def test_legacy_db_is_stamped_not_recreated(tmp_path, monkeypatch):
    """A pre-alembic DB (tables, no alembic_version) is adopted via `stamp`, not re-created.

    Re-creating would raise "table already exists"; stamping must preserve existing rows.
    """
    import app.db as db
    import app.models  # noqa: F401 — register metadata
    from app.db import Base

    path = f"{tmp_path}/legacy.db"
    # Build a legacy schema with create_all (the old world) + a row, no alembic_version.
    eng = _sync_engine(path)
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO owners (id, label) VALUES ('local', 'Legacy')"))
    assert "alembic_version" not in set(inspect(eng).get_table_names())

    _use_db(monkeypatch, path)
    await db.init_db()  # should stamp, not re-create

    insp = inspect(eng)
    assert "alembic_version" in set(insp.get_table_names())
    with eng.connect() as conn:
        # existing data survived (stamp doesn't touch app tables)
        assert conn.execute(text("SELECT label FROM owners WHERE id='local'")).scalar() == "Legacy"
    eng.dispose()


def test_models_match_head_migration_no_drift(tmp_path, monkeypatch):
    """Drift guard: the models must equal the latest migration.

    If someone adds/changes a model column without a new migration, alembic's
    compare_metadata returns diffs and this fails — the modern replacement for the
    old hand-maintained _ADDITIVE_COLUMNS map.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    import app.db as db
    import app.models  # noqa: F401
    from alembic import command
    from app.db import Base

    path = f"{tmp_path}/drift.db"
    _use_db(monkeypatch, path)
    # Upgrade a fresh DB to head via the real migration scripts.
    command.upgrade(db._alembic_config(), "head")

    eng = _sync_engine(path)
    with eng.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "render_as_batch": True}
        )
        diffs = compare_metadata(ctx, Base.metadata)
    eng.dispose()
    assert diffs == [], f"models diverge from head migration — generate a new revision: {diffs}"


@pytest.mark.asyncio
async def test_b1c2d3e4f5a6_settles_stranded_deferrals_and_nothing_else(tmp_path, monkeypatch):
    """P-0112/D-0076: rows already on disk are settled, precisely.

    The code change makes a null `deferred_until` unwritable; it does nothing for the
    47 rows that were already stranded on the always-on testbed. The `before_flush`
    guard only fires on write, and an unschedulable run is exactly the thing nothing
    ever writes to again — so without the migration they sit there forever.

    The assertion that matters is the *narrowness*: a working deferral must survive.
    """
    from sqlalchemy import text

    import app.db as db

    path = f"{tmp_path}/settle.db"
    _use_db(monkeypatch, path)
    await db.init_db()

    eng = _sync_engine(path)
    # Owner + task through the ORM so column defaults are applied; the *runs* go in
    # as raw SQL because the stranded shape is, by design, one the ORM now refuses
    # to write (that refusal is the P-0112 guard, tested separately).
    from sqlalchemy.orm import Session as SyncSession

    from app.models import Owner, Task

    with SyncSession(eng) as s:
        s.add(Owner(id="local", label="T"))
        s.add(Task(id=1, owner_id="local", name="t", prompt_template="p"))
        s.commit()

    cols = "id, owner_id, task_id, trigger, status, retry_count, overflow_used, " \
           "tokens_in, tokens_out, cost_usd, subagents, tool_calls, created_at, " \
           "deferred_until, error"
    with eng.begin() as conn:
        # 1: the stranded shape — deferred, no resolution time.
        # 2: a *working* deferral, which must be left completely alone.
        # 3: an unrelated failure, so the downgrade cannot resurrect it.
        conn.execute(text(
            f"INSERT INTO runs ({cols}) VALUES "
            "(1, 'local', 1, 'schedule', 'deferred', 0, 0, 0, 0, 0.0, 0, 0, "
            "  '2026-09-01 00:00:00', NULL, NULL),"
            "(2, 'local', 1, 'schedule', 'deferred', 0, 0, 0, 0, 0.0, 0, 0, "
            "  '2026-09-01 00:00:00', '2099-01-01 00:00:00', NULL),"
            "(3, 'local', 1, 'schedule', 'failed', 0, 0, 0, 0, 0.0, 0, 0, "
            "  '2026-09-01 00:00:00', NULL, 'something else entirely')"
        ))

    # Re-run the migration chain against the seeded rows.
    from alembic import command

    cfg = db._alembic_config()
    command.downgrade(cfg, "a9b8c7d6e5f4")
    command.upgrade(cfg, "b1c2d3e4f5a6")

    with eng.begin() as conn:
        rows = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(text(
                "SELECT id, status, error, finished_at FROM runs ORDER BY id"
            ))
        }

    assert rows[1][0] == "failed", "a run the sweep could never see must not stay 'deferred'"
    assert "never schedulable" in rows[1][1], "say what happened, so it is not read as a new break"
    assert rows[1][2] is not None, "a settled run is finished"

    assert rows[2][0] == "deferred", "a deferral WITH a time is a working deferral — untouched"
    assert rows[2][1] is None

    assert rows[3][1] == "something else entirely", "unrelated failures are not rewritten"

    # And the downgrade returns exactly what the upgrade changed.
    command.downgrade(cfg, "a9b8c7d6e5f4")
    with eng.begin() as conn:
        back = {
            r[0]: (r[1], r[2])
            for r in conn.execute(text("SELECT id, status, error FROM runs ORDER BY id"))
        }
    assert back[1] == ("deferred", None), "the marker makes the downgrade exact"
    assert back[2][0] == "deferred"
    assert back[3] == ("failed", "something else entirely"), (
        "a run that failed for its own reasons is never resurrected"
    )
