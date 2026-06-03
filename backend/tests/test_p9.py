"""
tests/test_p9.py — P9: stored BYO credentials are resolved and used by executors.

Tests:
- resolve_api_key precedence: stored key > env var > None
- ModelExecutor emits a clear 'no credentials' error (no network) when none configured
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.providers.base import EventKind
from app.providers.registry import get_provider_def


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


class TestResolveApiKey:
    @pytest.mark.asyncio
    async def test_stored_key_takes_precedence_over_env(self, fresh_db, monkeypatch):
        _, Session, _ = fresh_db
        import app.db as db_mod
        from app.credentials import store_credential, resolve_api_key

        orig = db_mod.AsyncSessionLocal
        db_mod.AsyncSessionLocal = Session
        try:
            async with Session() as db:
                await store_credential(db, "local", "openai-api", "sk-stored")
            monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

            key = await resolve_api_key("openai-api", "OPENAI_API_KEY")
            assert key == "sk-stored"  # stored wins
        finally:
            db_mod.AsyncSessionLocal = orig

    @pytest.mark.asyncio
    async def test_env_fallback_when_no_stored_key(self, fresh_db, monkeypatch):
        _, Session, _ = fresh_db
        import app.db as db_mod
        from app.credentials import resolve_api_key

        orig = db_mod.AsyncSessionLocal
        db_mod.AsyncSessionLocal = Session
        try:
            monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
            key = await resolve_api_key("openai-api", "OPENAI_API_KEY")
            assert key == "sk-from-env"
        finally:
            db_mod.AsyncSessionLocal = orig

    @pytest.mark.asyncio
    async def test_none_when_neither_present(self, fresh_db, monkeypatch):
        _, Session, _ = fresh_db
        import app.db as db_mod
        from app.credentials import resolve_api_key

        orig = db_mod.AsyncSessionLocal
        db_mod.AsyncSessionLocal = Session
        try:
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            key = await resolve_api_key("openai-api", "OPENAI_API_KEY")
            assert key is None
        finally:
            db_mod.AsyncSessionLocal = orig


class TestProviderConnected:
    @pytest.mark.asyncio
    async def test_mock_always_connected(self):
        from app.providers.registry import get_provider_def, is_provider_connected
        assert await is_provider_connected(get_provider_def("mock")) is True

    @pytest.mark.asyncio
    async def test_api_provider_disconnected_without_key(self, fresh_db, monkeypatch):
        _, Session, _ = fresh_db
        import app.db as db_mod
        from app.providers.registry import get_provider_def, is_provider_connected

        orig = db_mod.AsyncSessionLocal
        db_mod.AsyncSessionLocal = Session
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        try:
            assert await is_provider_connected(get_provider_def("openai-api")) is False
            monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
            assert await is_provider_connected(get_provider_def("openai-api")) is True
        finally:
            db_mod.AsyncSessionLocal = orig


class TestNewApiProviders:
    def test_grok_and_gemini_registered_as_openai_compat(self):
        from app.providers.registry import get_provider_def
        grok = get_provider_def("grok-api")
        gem = get_provider_def("gemini-api")
        assert grok and grok.kind == "openai_compatible" and grok.env_key == "XAI_API_KEY"
        assert grok.base_url and "x.ai" in grok.base_url
        assert gem and gem.kind == "openai_compatible" and gem.env_key == "GEMINI_API_KEY"
        assert gem.base_url and "googleapis.com" in gem.base_url

    @pytest.mark.asyncio
    async def test_connection_follows_their_own_env_keys(self, fresh_db, monkeypatch):
        _, Session, _ = fresh_db
        import app.db as db_mod
        from app.providers.registry import get_provider_def, is_provider_connected

        orig = db_mod.AsyncSessionLocal
        db_mod.AsyncSessionLocal = Session
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        try:
            assert await is_provider_connected(get_provider_def("grok-api")) is False
            assert await is_provider_connected(get_provider_def("gemini-api")) is False
            monkeypatch.setenv("XAI_API_KEY", "xai-key")
            monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
            assert await is_provider_connected(get_provider_def("grok-api")) is True
            assert await is_provider_connected(get_provider_def("gemini-api")) is True
        finally:
            db_mod.AsyncSessionLocal = orig


class TestExecutorNoCredentials:
    @pytest.mark.asyncio
    async def test_model_executor_errors_without_key(self, fresh_db, monkeypatch):
        """With no stored key and no env var, the executor must emit a clean
        error event (not attempt a network call / crash)."""
        _, Session, _ = fresh_db
        import app.db as db_mod
        from app.providers.model_executor import ModelExecutor

        orig = db_mod.AsyncSessionLocal
        db_mod.AsyncSessionLocal = Session
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        try:
            pdef = get_provider_def("openai-api")
            assert pdef is not None
            ex = ModelExecutor(pdef)
            events = []
            async for ev in ex.run_stream("hi", workdir="/tmp", tools_enabled=False):
                events.append(ev)
            assert any(e.kind == EventKind.error for e in events)
            err = next(e for e in events if e.kind == EventKind.error)
            assert "no credentials" in (err.message or "")
            # must not have produced a result
            assert not any(e.kind == EventKind.result for e in events)
        finally:
            db_mod.AsyncSessionLocal = orig
