"""
db.py — async SQLAlchemy engine + session factory + schema migration entrypoint.

Schema is owned by **alembic** (D-0021): `init_db()` brings the DB to head. This
replaced the previous hand-rolled, SQLite-only additive-column backfill — alembic
works for both the current per-user SQLite (data plane) and a future Postgres
control plane. Tests build their schema directly from `Base.metadata.create_all`
(the model metadata is the source the alembic baseline is generated from), so they
do not pay the migration cost.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


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


# ── Schema migrations (alembic, D-0021) ──────────────────────────────────────
# `init_db()` brings the schema to head. Three startup states are handled so the
# transition from the old create_all+backfill world is seamless:
#   • fresh DB                         → `upgrade head` creates every table.
#   • already alembic-managed DB       → `upgrade head` applies any new revisions.
#   • legacy DB (tables but no
#     alembic_version — created by the
#     old create_all+backfill)         → `stamp head`: it is structurally already
#                                         at head (the old backfill kept it current),
#                                         so adopt it without re-creating tables.

_ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")


def _alembic_config():
    from alembic.config import Config

    cfg = Config(_ALEMBIC_INI)
    # script_location in the ini is relative to the ini's dir; make it absolute so
    # init_db works regardless of the process CWD (uvicorn, `python -m app.seed`, tests).
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(_ALEMBIC_INI), "alembic"))
    return cfg


def _sync_url() -> str:
    """DATABASE_URL with the async driver stripped (migrations run on a sync engine)."""
    return get_settings().database_url.replace("+aiosqlite", "").replace("+asyncpg", "")


def _run_migrations() -> None:
    """Synchronous migration runner (called via asyncio.to_thread from init_db)."""
    from sqlalchemy import create_engine, inspect

    from alembic import command

    cfg = _alembic_config()
    sync_engine = create_engine(_sync_url())
    try:
        with sync_engine.connect() as conn:
            names = set(inspect(conn).get_table_names())
        app_tables = names - {"alembic_version"}
        if "alembic_version" not in names and app_tables:
            # Legacy pre-alembic DB already at head schema → adopt it in place.
            logger.info("[db] legacy schema (%d tables) — stamping alembic head", len(app_tables))
            command.stamp(cfg, "head")
        else:
            command.upgrade(cfg, "head")
    finally:
        sync_engine.dispose()


async def init_db() -> None:
    """Bring the database schema to the latest alembic revision (see states above)."""
    await asyncio.to_thread(_run_migrations)
