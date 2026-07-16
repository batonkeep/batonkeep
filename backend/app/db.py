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


# ── SQLite write-hardening (D-0058 A1) ────────────────────────────────────────
# Every writer previously used SQLite defaults: no busy_timeout (a concurrent
# commit → immediate "database is locked" 500) and rollback-journal mode (writers
# block readers). WAL + a 5s busy_timeout is the correct local-filesystem setup;
# the one place WAL is *wrong* is a network filesystem (its -shm mmap needs
# coherent shared memory NFS/CIFS don't provide), so journal mode is selectable
# and auto-detection falls back to TRUNCATE there. All pragmas are dialect-
# conditional: the future Postgres path (§6) must stay a DATABASE_URL change.

_SQLITE_BUSY_TIMEOUT_MS = 5000
_NETWORK_FS_TYPES = ("nfs", "cifs", "smbfs", "smb3", "fuse.sshfs", "9p", "afs")


def _sqlite_journal_mode(db_path: str) -> str:
    """Pick the journal mode: explicit setting wins, else WAL unless the DB dir
    sits on a network filesystem (best-effort /proc/mounts check; non-Linux and
    unreadable-mounts default to WAL — local container/laptop filesystems)."""
    override = get_settings().sqlite_journal_mode.strip().lower()
    if override in ("wal", "truncate", "delete"):
        return override
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            mounts = [line.split() for line in f]
        db_dir = os.path.dirname(os.path.abspath(db_path)) or "/"
        best = ("", "")
        for parts in mounts:
            if len(parts) >= 3 and (db_dir + "/").startswith(parts[1].rstrip("/") + "/"):
                if len(parts[1]) > len(best[0]):
                    best = (parts[1], parts[2])
        if any(best[1].startswith(t) for t in _NETWORK_FS_TYPES):
            logger.warning(
                "[db] %s is on a network filesystem (%s) — using TRUNCATE journal "
                "(WAL is unsafe over network mounts); prefer a local data volume",
                db_path, best[1],
            )
            return "truncate"
    except OSError:
        pass
    return "wal"


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
    eng = create_async_engine(
        url,
        echo=settings.log_level == "DEBUG",
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
    )
    if url.startswith("sqlite"):
        from sqlalchemy import event

        db_path = url.split("///")[-1]
        journal_mode = _sqlite_journal_mode(db_path)

        @event.listens_for(eng.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute(f"PRAGMA journal_mode = {journal_mode}")
            if journal_mode == "wal":
                # Standard WAL pairing: fsync at checkpoint, not every commit.
                # Durable across crashes; the power-loss window is acceptable
                # for operational state (canonical truth lives in files/Git).
                cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

    return eng


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
    url = _sync_url()
    # Migrations get the same busy-timeout protection (sqlite3 `timeout` is the
    # busy handler in seconds) — an app writer mid-commit must not 500 a startup.
    connect_args = {"timeout": _SQLITE_BUSY_TIMEOUT_MS / 1000} if url.startswith("sqlite") else {}
    sync_engine = create_engine(url, connect_args=connect_args)
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
