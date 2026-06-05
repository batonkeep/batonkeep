"""
tests/test_p9_sensitivity.py — P-0009 #1: sensitivity-aware routing.

The sovereignty boundary: a task marked `sensitivity: confidential` may only
ever resolve to a *local* provider, and must fail closed (defer) rather than
fall back to any remote API/CLI.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.providers.registry import (
    get_provider_def,
    is_local_instance,
    local_candidate_ids,
)
from app.quota import QuotaTracker
from app.router import CandidatePlan, DeferredResult, resolve
from app.sessions.orchestrator import SessionError, enforce_local_if_confidential


def fresh_quota() -> QuotaTracker:
    return QuotaTracker()


def _confidential(candidates=None, tags=None, overflow_to=None) -> dict:
    return {
        "strategy": "capability",
        "sensitivity": "confidential",
        "candidates": candidates or ["mock"],
        "capability_tags": tags or [],
        "overflow_to": overflow_to,
    }


# ── Registry wiring ────────────────────────────────────────────────────────────

class TestLocalProviderWiring:
    def test_ollama_is_registered_and_local(self):
        pdef = get_provider_def("ollama")
        assert pdef is not None
        assert pdef.local is True
        assert pdef.tier == "open"
        assert "local" in pdef.capability_tags

    def test_remote_providers_are_not_local(self):
        for name in ("claude-api", "openai-api", "grok-api", "gemini-api", "open-default", "mock"):
            pdef = get_provider_def(name)
            assert pdef is not None and pdef.local is False, name

    def test_local_candidate_ids_lists_only_local(self):
        ids = local_candidate_ids()
        assert "ollama" in ids
        assert "claude-api" not in ids
        assert "mock" not in ids


# ── Confidential routing policy ────────────────────────────────────────────────

class TestConfidentialRouting:
    def test_routes_only_to_local_ignoring_declared_remotes(self):
        """Even if the task declares remote candidates, confidential work goes local."""
        q = fresh_quota()
        result = resolve(_confidential(candidates=["claude-api", "openai-api"]), q)
        assert isinstance(result, CandidatePlan)
        assert result.candidates == ["ollama"]

    def test_overflow_is_forbidden_offbox(self):
        q = fresh_quota()
        result = resolve(_confidential(candidates=["mock"], overflow_to="claude-api"), q)
        assert isinstance(result, CandidatePlan)
        assert result.overflow_to is None

    def test_fails_closed_when_local_is_cooling(self):
        """No healthy local provider → defer, never fall back to a remote."""
        q = fresh_quota()
        q.mark_cooldown("ollama", reset_at=datetime.now(UTC) + timedelta(minutes=30))
        result = resolve(_confidential(), q)
        assert isinstance(result, DeferredResult)
        assert "ollama" in result.cooling_providers

    def test_non_confidential_does_not_pull_in_local(self):
        """A normal task is unaffected — ollama isn't silently added."""
        q = fresh_quota()
        result = resolve({"strategy": "capability", "candidates": ["mock"]}, q)
        assert isinstance(result, CandidatePlan)
        assert result.candidates == ["mock"]
        assert "ollama" not in result.candidates


# ── Confidential session pin (build sessions, not the Task router) ──────────────

class TestConfidentialSessionPin:
    def test_is_local_instance(self):
        assert is_local_instance("ollama") is True
        assert is_local_instance("mock") is False
        assert is_local_instance("claude-api") is False
        assert is_local_instance("does-not-exist") is False

    def test_non_confidential_keeps_chosen_provider(self):
        assert enforce_local_if_confidential("mock", False) == "mock"
        assert enforce_local_if_confidential("claude-api", False) == "claude-api"

    def test_confidential_keeps_an_already_local_choice(self):
        assert enforce_local_if_confidential("ollama", True) == "ollama"

    def test_confidential_overrides_a_remote_choice_to_local(self):
        # a remote selection on a confidential session is pinned back to local
        assert enforce_local_if_confidential("claude-api", True) == "ollama"
        assert enforce_local_if_confidential("mock", True) == "ollama"

    def test_confidential_fails_closed_when_no_local_available(self, monkeypatch):
        import app.sessions.orchestrator as orch
        monkeypatch.setattr(orch, "local_candidate_ids", lambda: [])
        monkeypatch.setattr(orch, "is_local_instance", lambda _id: False)
        with pytest.raises(SessionError):
            enforce_local_if_confidential("claude-api", True)


# ── Confidential toggle over HTTP ───────────────────────────────────────────────

class TestConfidentialSessionApi:
    def test_create_and_toggle_confidential(self, tmp_path, monkeypatch):
        import asyncio

        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base, get_db
        from app.main import _owner_id, app
        from app.models import Owner
        from app.sessions import workspace as ws_mod

        monkeypatch.setattr(ws_mod._settings, "sessions_dir", str(tmp_path / "sessions"))
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                db.add(Owner(id="local", label="Test"))
                await db.commit()
            return maker

        maker = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override_db():
            async with maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)
            created = c.post("/api/sessions", json={"title": "Secret", "confidential": True})
            assert created.status_code == 201
            sid = created.json()["id"]
            assert created.json()["confidential"] is True

            # default is off
            other = c.post("/api/sessions", json={"title": "Public"})
            assert other.json()["confidential"] is False

            # toggle off via PATCH
            patched = c.patch(f"/api/sessions/{sid}", json={"confidential": False})
            assert patched.status_code == 200
            assert patched.json()["confidential"] is False
        finally:
            app.dependency_overrides.clear()
