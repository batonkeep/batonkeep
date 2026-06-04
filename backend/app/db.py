"""
db.py — async SQLAlchemy engine + session factory.
"""
from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    # Ensure the directory exists for SQLite (best-effort; skip on read-only FS in tests)
    if url.startswith("sqlite"):
        db_path = url.split("///")[-1]
        db_dir = os.path.dirname(db_path)
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
            except OSError:
                pass  # read-only FS in tests / containers — engine creation will still succeed
    return create_async_engine(
        url,
        echo=settings.log_level == "DEBUG",
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
    )


engine = _make_engine()
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# Lightweight additive migrations for SQLite (this project has no alembic).
# create_all() creates *missing tables* but never alters *existing* ones, so a
# column added to a model after a DB already exists on a persisted volume must be
# backfilled here — otherwise queries over that table fail with "no such column"
# against the stale schema (e.g. M1.3 added session_turns.commit_sha/diffstat).
# Additive + idempotent only: add columns, never drop/alter. Non-SQLite engines
# are left to their own migration tooling.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # table -> {column: column_type_ddl}
    "session_turns": {
        "commit_sha": "VARCHAR(40)",
        "diffstat": "TEXT",
    },
}


async def _backfill_additive_columns(conn) -> None:
    """Add any model columns missing from an existing SQLite table (idempotent)."""
    for table, columns in _ADDITIVE_COLUMNS.items():
        rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).fetchall()
        if not rows:
            continue  # table doesn't exist yet (create_all handles fresh DBs)
        existing = {row[1] for row in rows}  # row[1] = column name
        for col, ddl in columns.items():
            if col not in existing:
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


async def init_db() -> None:
    """Create missing tables, then backfill additive columns on existing ones."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "sqlite":
            await _backfill_additive_columns(conn)
