"""
alembic/env.py — migration environment.

The database URL is derived from the application settings (a *sync* driver form of
DATABASE_URL) so the app and migrations share one source of truth. We deliberately do
NOT call logging.fileConfig here — the app owns logging (app/logging_config.py).
"""
from __future__ import annotations

from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401 — registers all ORM tables on Base.metadata
from alembic import context

# Make the app importable and pull in the model metadata.
from app.config import get_settings
from app.db import Base

config = context.config

target_metadata = Base.metadata


def _sync_url() -> str:
    """The configured DATABASE_URL with the async driver stripped for migrations.

    `sqlite+aiosqlite://` → `sqlite://`; `postgresql+asyncpg://` → `postgresql://`.
    Migrations run on a plain sync engine (simpler + identical schema result).
    """
    url = get_settings().database_url
    return url.replace("+aiosqlite", "").replace("+asyncpg", "")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,  # SQLite needs batch mode to ALTER columns
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,  # batch mode → ALTER works on SQLite
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
