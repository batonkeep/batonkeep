"""
tests/test_db_migrations.py — additive SQLite migrations on startup.

Regression: M1.3 added session_turns.commit_sha/diffstat. On a persisted volume
(e.g. after a Docker rebuild) create_all() leaves the existing table untouched, so
queries over SessionTurn failed with "no such column" and the chat history
vanished while the git-backed version history still worked. init_db() must
backfill missing additive columns on an existing SQLite DB.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_init_db_backfills_missing_columns(tmp_path, monkeypatch):
    import app.db as db

    url = f"sqlite+aiosqlite:///{tmp_path}/stale.db"
    engine = create_async_engine(url)

    # Simulate a pre-M1.3 schema: session_turns WITHOUT commit_sha / diffstat.
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE session_turns ("
            "id INTEGER PRIMARY KEY, session_id VARCHAR, owner_id VARCHAR, "
            "seq INTEGER, provider VARCHAR, prompt TEXT, response TEXT, "
            "status VARCHAR, error TEXT, created_at DATETIME, finished_at DATETIME)"
        )

    # Point init_db at this stale engine and run the startup path.
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()

    async with engine.begin() as conn:
        cols = {
            row[1]
            for row in (await conn.exec_driver_sql("PRAGMA table_info(session_turns)")).fetchall()
        }
    assert "commit_sha" in cols and "diffstat" in cols

    # Idempotent: a second run doesn't error or duplicate columns.
    await db.init_db()
    await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_backfills_credentials_label(tmp_path, monkeypatch):
    """
    Regression: credentials.label was added to the model after early DBs existed,
    but wasn't in the backfill map — so credential reads (e.g. the Cloudflare
    connector) hit "no such column: credentials.label" on a persisted volume.
    """
    import app.db as db

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/stale_creds.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE credentials ("
            "id INTEGER PRIMARY KEY, owner_id VARCHAR, provider VARCHAR, "
            "ciphertext TEXT, created_at DATETIME)"  # pre-label schema
        )

    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()

    async with engine.begin() as conn:
        cols = {
            row[1]
            for row in (await conn.exec_driver_sql("PRAGMA table_info(credentials)")).fetchall()
        }
    assert "label" in cols
    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_is_noop_on_fresh_db(tmp_path, monkeypatch):
    """A fresh DB gets columns from create_all; backfill finds nothing to add."""
    import app.db as db
    from app.db import Base
    # Register all models on the metadata.
    import app.models  # noqa: F401

    url = f"sqlite+aiosqlite:///{tmp_path}/fresh.db"
    engine = create_async_engine(url)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()

    async with engine.begin() as conn:
        cols = {
            row[1]
            for row in (await conn.exec_driver_sql("PRAGMA table_info(session_turns)")).fetchall()
        }
    assert {"commit_sha", "diffstat"} <= cols
    await engine.dispose()
