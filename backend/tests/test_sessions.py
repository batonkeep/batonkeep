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


# ── M1.3: artifacts + versioning ──────────────────────────────────────────────
#
# Verify gate (PLAN §M1.3): successive builds create per-turn commits; diff/
# rollback (checkout) works; per-turn diffs surface in the event view; artifacts
# are owner_id-scoped.

class TestWorkspaceVersioning:
    @pytest.mark.asyncio
    async def test_commit_turn_creates_version_with_diff(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("v1", title="T", goal="G")

        with open(os.path.join(root, "index.html"), "w") as f:
            f.write("<h1>hi</h1>")
        version = await ws.commit_turn(root, seq=0, provider="mock", summary="built page")

        assert version is not None
        assert len(version["commit"]) == 40
        assert "turn 0 (mock)" in version["message"]
        assert "index.html" in version["diff"]
        assert "index.html" in version["diffstat"]

    @pytest.mark.asyncio
    async def test_commit_turn_surfaces_per_file_artifacts(self, tmp_path, monkeypatch):
        """D-0017 thread 2: the turn result is the per-file artifact list."""
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("vf", title="T", goal="G")

        with open(os.path.join(root, "index.html"), "w") as f:
            f.write("<h1>one</h1>\n<p>two</p>\n")
        v0 = await ws.commit_turn(root, seq=0, provider="mock", summary="page")
        files = {f["path"]: f for f in v0["files"]}
        # SESSION.md churns every turn but is an internal ledger — excluded.
        assert "SESSION.md" not in files
        assert files["index.html"]["status"] == "added"
        assert files["index.html"]["additions"] == 2
        assert files["index.html"]["deletions"] == 0

        # A second turn: modify one file, add one, remove one.
        with open(os.path.join(root, "index.html"), "w") as f:
            f.write("<h1>one</h1>\n")            # one line removed
        with open(os.path.join(root, "style.css"), "w") as f:
            f.write("body{}\n")                   # added
        v1 = await ws.commit_turn(root, seq=1, provider="mock", summary="edit")
        files = {f["path"]: f for f in v1["files"]}
        assert files["index.html"]["status"] == "changed"
        assert files["index.html"]["deletions"] == 1
        assert files["style.css"]["status"] == "added"

    @pytest.mark.asyncio
    async def test_commit_turn_noop_when_nothing_changed(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("v2", title="T", goal="G")
        # No file edits after init → no new version.
        assert await ws.commit_turn(root, seq=0, provider="mock") is None

    @pytest.mark.asyncio
    async def test_list_versions_newest_first(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("v3", title="T", goal="G")
        for i in range(2):
            with open(os.path.join(root, "index.html"), "w") as f:
                f.write(f"<h1>v{i}</h1>")
            await ws.commit_turn(root, seq=i, provider="mock", summary=f"edit {i}")

        versions = await ws.list_versions(root)
        # 2 turn commits + the initial workspace commit, newest first.
        assert len(versions) == 3
        assert "turn 1 (mock)" in versions[0]["message"]
        assert "initialise workspace" in versions[-1]["message"]

    @pytest.mark.asyncio
    async def test_restore_version_checks_out_and_recommits(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("v4", title="T", goal="G")

        page = os.path.join(root, "index.html")
        with open(page, "w") as f:
            f.write("<h1>first</h1>")
        v0 = await ws.commit_turn(root, seq=0, provider="mock", summary="first")
        with open(page, "w") as f:
            f.write("<h1>second</h1>")
        await ws.commit_turn(root, seq=1, provider="mock", summary="second")

        # Roll back to v0: the file content returns and a new restore version lands.
        result = await ws.restore_version(root, v0["commit"])
        assert result is not None
        assert result["restored_from"] == v0["commit"]
        with open(page) as f:
            assert f.read() == "<h1>first</h1>"
        # History preserved + extended (restore is itself undoable).
        assert len(await ws.list_versions(root)) == 4

    @pytest.mark.asyncio
    async def test_restore_removes_files_added_after_target(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("v5", title="T", goal="G")

        with open(os.path.join(root, "a.html"), "w") as f:
            f.write("a")
        v0 = await ws.commit_turn(root, seq=0, provider="mock", summary="a")
        with open(os.path.join(root, "b.html"), "w") as f:
            f.write("b")
        await ws.commit_turn(root, seq=1, provider="mock", summary="b")

        await ws.restore_version(root, v0["commit"])
        assert not os.path.exists(os.path.join(root, "b.html"))  # clean removed it
        assert os.path.exists(os.path.join(root, "a.html"))

    @pytest.mark.asyncio
    async def test_restore_rejects_unknown_or_unsafe_ref(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setitem(ws._settings.__dict__, "sessions_dir", str(tmp_path))
        root = await ws.create_workspace("v6", title="T", goal="G")
        assert await ws.restore_version(root, "HEAD~1") is None        # refspec rejected
        assert await ws.restore_version(root, "deadbeef" * 5) is None   # unknown sha
        assert await ws.version_diff(root, "main") is None             # refspec rejected


class TestTurnVersioning:
    @pytest.mark.asyncio
    async def test_turn_records_commit_and_broadcasts_diff(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        root = await _make_session(Maker, ws)

        turn_id = await orch.run_turn("s1", "build a hero section", owner_id="local")

        from app.models import SessionTurn
        async with Maker() as db:
            turn = await db.get(SessionTurn, turn_id)
            assert turn.commit_sha and len(turn.commit_sha) == 40
            assert turn.diffstat and "index.html" in turn.diffstat
            # D-0017 thread 2: per-file artifacts persisted as the turn result.
            import json
            changed = json.loads(turn.changed_files)
            assert any(f["path"] == "index.html" for f in changed)

        # Per-turn diff surfaced in the live event stream (event view gate).
        version_events = [
            b for b in broadcasts
            if b["type"] == "session.event" and b["event"].get("phase") == "version"
        ]
        assert version_events
        assert "index.html" in version_events[0]["event"]["data"]["diff"]
        # ...and the per-file artifact list rides the same event (D-0017 thread 2).
        ev_files = version_events[0]["event"]["data"]["files"]
        assert any(f["path"] == "index.html" for f in ev_files)

        # The commit is a real version in workspace history.
        versions = await ws.list_versions(root)
        assert any(v["commit"] == turn.commit_sha for v in versions)


class TestVersioningHTTP:
    """versions / diff / restore endpoints + owner_id isolation (M1.3 gate)."""

    def test_versions_diff_restore_and_owner_isolation(self, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id
        from app.sessions import workspace as ws

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/v.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            # Build two workspaces with one committed version each, owned separately.
            root_a = await ws.create_workspace("sa", title="A", goal="G")
            with open(os.path.join(root_a, "index.html"), "w") as f:
                f.write("<h1>alpha</h1>")
            await ws.commit_turn(root_a, seq=0, provider="mock", summary="alpha")
            with open(os.path.join(root_a, "index.html"), "w") as f:
                f.write("<h1>beta</h1>")
            await ws.commit_turn(root_a, seq=1, provider="mock", summary="beta")
            root_b = await ws.create_workspace("sb", title="B", goal="G")
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add_all([
                    Owner(id="local", label="Me"),
                    Owner(id="other", label="Them"),
                    SessionModel(id="sa", owner_id="local", title="A", provider="mock",
                                 workspace_path=root_a, preview_token="t", status="active"),
                    SessionModel(id="sb", owner_id="other", title="B", provider="mock",
                                 workspace_path=root_b, preview_token="t", status="active"),
                ])
                await db.commit()
            return Maker

        Maker = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)

            versions = c.get("/api/sessions/sa/versions")
            assert versions.status_code == 200
            vs = versions.json()
            assert len(vs) == 3  # init + 2 turns
            assert "turn 1 (mock)" in vs[0]["message"]
            first_turn_commit = vs[1]["commit"]

            # Per-turn diff.
            diff = c.get(f"/api/sessions/sa/versions/{first_turn_commit}/diff")
            assert diff.status_code == 200
            assert "alpha" in diff.json()["diff"]

            # Rollback to the first turn restores its content as a new version.
            restored = c.post("/api/sessions/sa/restore", json={"commit": first_turn_commit})
            assert restored.status_code == 200
            assert restored.json()["restored_from"] == first_turn_commit
            assert len(c.get("/api/sessions/sa/versions").json()) == 4

            # owner_id isolation: another owner's session is invisible (404), not
            # leaked through any of the three routes.
            assert c.get("/api/sessions/sb/versions").status_code == 404
            assert c.get(f"/api/sessions/sb/versions/{first_turn_commit}/diff").status_code == 404
            assert c.post("/api/sessions/sb/restore",
                          json={"commit": first_turn_commit}).status_code == 404
        finally:
            app.dependency_overrides.clear()
            ws._settings.__dict__.pop("sessions_dir", None)
            asyncio.get_event_loop().run_until_complete(engine.dispose())
