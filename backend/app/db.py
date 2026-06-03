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


async def init_db() -> None:
    """Create all tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
