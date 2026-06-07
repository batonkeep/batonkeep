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
    import app.db as db
    import app.models  # noqa: F401
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
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
