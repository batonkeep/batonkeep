"""
tests/test_sessions.py — M1.1 gate: build sessions + workspace.

Verify gate (PLAN §M1.1):
  a mock-agent session edits files in an isolated git-init'd workspace; switching
  provider mid-conversation routes the next turn to a different executor and the
  new agent continues from the workspace + SESSION.md (not a replayed transcript);
  events stream live.
"""
from __future__ import annotations

import os

import pytest
from unittest.mock import AsyncMock

from app.providers.mock import MockExecutor


# ── Workspace unit tests ──────────────────────────────────────────────────────

class TestWorkspace:
    @pytest.mark.asyncio
    async def test_create_workspace_is_git_initd_with_brief(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))

        root = await ws.create_workspace("sess1", title="Landing page", goal="ship a site")

        assert os.path.isdir(root)
        assert os.path.isdir(os.path.join(root, ".git"))            # git-init'd
        assert os.path.exists(os.path.join(root, ws.BRIEF_FILENAME)) # SESSION.md brief
        assert "Landing page" in ws.read_brief(root)

    def test_safe_join_blocks_traversal(self, tmp_path):
        from app.sessions import workspace as ws
        root = str(tmp_path)
        assert ws.safe_join(root, "assets/logo.png").startswith(root)
        with pytest.raises(ValueError):
            ws.safe_join(root, "../escape.txt")
        with pytest.raises(ValueError):
            ws.safe_join(root, "/etc/passwd")

    def test_workspace_root_rejects_unsafe_id(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        with pytest.raises(ValueError):
            ws.workspace_root("../../etc")

    @pytest.mark.asyncio
    async def test_turn_context_uses_workspace_not_transcript(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("sess2", title="T", goal="G")
        ws.append_progress(root, "turn 0 (grok): built hero section")

        ctx = ws.build_turn_context(root, "make the hero bigger")
        assert "built hero section" in ctx     # from SESSION.md brief
        assert "SESSION.md" in ctx              # workspace file listing
        assert "make the hero bigger" in ctx    # the new user message


# ── Session turn lifecycle (Verify gate) ──────────────────────────────────────

@pytest.fixture
async def session_env(tmp_path):
    """Fresh DB + patched session orchestrator/workspace pointing at tmp_path."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models import Owner, Session as SessionModel  # register metadata
    from app.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="Test"))
        await db.commit()

    import app.sessions.orchestrator as orch
    from app.sessions import workspace as ws

    orig_local = orch.AsyncSessionLocal
    orch.AsyncSessionLocal = Maker
    ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")

    # Capture WS broadcasts.
    broadcasts: list[dict] = []
    orig_broadcast = orch.ws_manager.broadcast

    async def _capture(payload):
        broadcasts.append(payload)

    orch.ws_manager.broadcast = _capture

    # Resolve any provider name to a distinct MockExecutor so we can show a switch.
    orig_get_exec = orch.get_executor
    orch.get_executor = lambda name: MockExecutor(name=name, latency_ms=1)

    yield Maker, ws, orch, broadcasts

    orch.AsyncSessionLocal = orig_local
    orch.ws_manager.broadcast = orig_broadcast
    orch.get_executor = orig_get_exec
    ws._settings.__dict__.pop("sessions_dir", None)
    await engine.dispose()


async def _make_session(Maker, ws, session_id="s1", provider="mock"):
    from app.models import Session as SessionModel
    root = await ws.create_workspace(session_id, title="Landing page", goal="ship a site")
    async with Maker() as db:
        db.add(SessionModel(
            id=session_id, owner_id="local", title="Landing page",
            provider=provider, workspace_path=root, status="active",
        ))
        await db.commit()
    return root


class TestSessionTurns:
    @pytest.mark.asyncio
    async def test_turn_edits_workspace_and_streams(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        root = await _make_session(Maker, ws)

        turn_id = await orch.run_turn("s1", "build a hero section", owner_id="local")

        # File actually edited in the isolated workspace.
        assert os.path.exists(os.path.join(root, "index.html"))
        with open(os.path.join(root, "index.html")) as f:
            assert "build a hero section" in f.read()

        # Turn persisted as succeeded.
        from app.models import SessionTurn
        async with Maker() as db:
            turn = await db.get(SessionTurn, turn_id)
            assert turn.status == "succeeded"
            assert turn.provider == "mock"
            assert turn.response

        # Events streamed live over WS.
        kinds = [b["type"] for b in broadcasts]
        assert "session.event" in kinds
        assert "session.turn.update" in kinds

    @pytest.mark.asyncio
    async def test_provider_switch_routes_to_different_executor(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        root = await _make_session(Maker, ws, provider="mock")

        # Turn 0 on the default provider.
        await orch.run_turn("s1", "start the page", owner_id="local")
        # Turn 1 switches provider mid-conversation.
        await orch.run_turn("s1", "now switch agents", provider="grok-mock", owner_id="local")

        from app.models import Session as SessionModel, SessionTurn
        from sqlalchemy import select
        async with Maker() as db:
            turns = (await db.execute(
                select(SessionTurn).where(SessionTurn.session_id == "s1").order_by(SessionTurn.seq)
            )).scalars().all()
            assert [t.provider for t in turns] == ["mock", "grok-mock"]  # routed to a different executor
            session = await db.get(SessionModel, "s1")
            assert session.provider == "grok-mock"                       # switch persisted

        # The new agent continued from the workspace + SESSION.md — the brief
        # carries both turns' progress, and a switch route event was emitted.
        brief = ws.read_brief(root)
        assert "turn 0 (mock)" in brief and "turn 1 (grok-mock)" in brief
        assert any(
            b["type"] == "session.event" and b["event"]["kind"] == "route"
            and "switched to grok-mock" in (b["event"]["message"] or "")
            for b in broadcasts
        )

    @pytest.mark.asyncio
    async def test_unknown_provider_raises(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        orch.get_executor = lambda name: None  # nothing available
        with pytest.raises(orch.SessionError):
            await orch.run_turn("s1", "hello", provider="nope", owner_id="local")


# ── Rename endpoint (HTTP) ────────────────────────────────────────────────────

class TestSessionRename:
    """PATCH /api/sessions/{id} renames the session; empty/whitespace is rejected."""

    def test_patch_renames_and_validates(self, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.get_event_loop().run_until_complete(_setup())
        Maker = async_sessionmaker(engine, expire_on_commit=False)

        async def _seed():
            async with Maker() as db:
                db.add(Owner(id="local", label="Test"))
                db.add(SessionModel(
                    id="s1", owner_id="local", title="Untitled session", provider="mock",
                    workspace_path=str(tmp_path), preview_token="tok", status="active",
                ))
                await db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)
            ok = c.patch("/api/sessions/s1", json={"title": "  Catering landing page  "})
            assert ok.status_code == 200
            assert ok.json()["title"] == "Catering landing page"  # trimmed

            assert c.patch("/api/sessions/s1", json={"title": "   "}).status_code == 400
            assert c.patch("/api/sessions/nope", json={"title": "x"}).status_code == 404
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())
