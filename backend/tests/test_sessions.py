"""
tests/test_sessions.py — M1.1 gate: build sessions + workspace.

Verify gate (PLAN §M1.1):
  a mock-agent session edits files in an isolated git-init'd workspace; switching
  provider mid-conversation routes the next turn to a different executor and the
  new agent continues from the workspace + SESSION.md (not a replayed transcript);
  events stream live.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.providers.base import EventKind, ExecEvent, Executor
from app.providers.mock import MockExecutor


class _WriteThenHangExecutor(Executor):
    """Writes a workspace file, emits a file_write tool event, then hangs — so a
    user interrupt or the session-turn timeout fires while a real, uncommitted diff
    exists in the tree (exercises P-0069 cancel-snapshot / timeout-snapshot)."""

    name = "hang"
    tier = "mock"

    def __init__(self, name: str = "hang", *, hang_s: float = 30.0) -> None:
        self.name = name
        self.tier = "mock"
        self._hang_s = hang_s

    @property
    def kind(self) -> str:
        return "mock"

    def is_healthy(self) -> bool:
        return True

    async def run_stream(self, prompt, *, workdir, tools_enabled=True,
                         max_rounds=10, budget_usd=1.0, extra=None):
        yield ExecEvent(kind=EventKind.phase, phase="running", message="[hang] running")
        with open(os.path.join(workdir, "index.html"), "a", encoding="utf-8") as f:
            f.write("<!-- partial work written before interrupt -->\n")
        yield ExecEvent(kind=EventKind.tool, message="[hang] wrote index.html",
                        data={"tool": "file_write", "path": "index.html"})
        await asyncio.sleep(self._hang_s)  # hang — never reaches a result event
        yield ExecEvent(kind=EventKind.result, message="unreachable", data={})


async def _wait_for_file_write(broadcasts, *, tries=300, delay=0.02):
    """Poll captured broadcasts until the hang executor's file_write is seen."""
    for _ in range(tries):
        await asyncio.sleep(delay)
        if any(
            (b.get("event") or {}).get("data", {}).get("tool") == "file_write"
            for b in broadcasts
        ):
            return True
    return False

# ── Workspace unit tests ──────────────────────────────────────────────────────

class TestWorkspace:
    @pytest.mark.asyncio
    async def test_create_workspace_is_git_initd_with_brief(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)

        root = await ws.create_workspace("sess1", title="Landing page", goal="ship a site")

        assert os.path.isdir(root)
        assert os.path.isdir(os.path.join(root, ".git"))            # git-init'd
        assert os.path.exists(os.path.join(root, ws.BRIEF_FILENAME)) # SESSION.md brief
        assert "Landing page" in ws.read_brief(root)

    @pytest.mark.asyncio
    async def test_record_turn_logs_structured_entry_with_artifacts(self, tmp_path, monkeypatch):
        """D-0017 thread 1: ledger entries are grounded in the turn's artifacts."""
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        root = await ws.create_workspace("led1", title="T", goal="G")

        ws.record_turn(
            root, seq=0, provider="claude", summary="built the hero\nand more",
            files=[{"path": "index.html", "status": "added", "additions": 12, "deletions": 0},
                   {"path": "style.css", "status": "changed", "additions": 3, "deletions": 1}],
            lane="chat",
        )
        brief = ws.read_brief(root)
        assert "_(none yet)_" not in brief                      # placeholder dropped
        assert "**turn 0** · claude · chat" in brief
        assert "built the hero" in brief and "and more" not in brief  # first line only
        assert "index.html (+12)" in brief
        assert "style.css (+3 −1)" in brief

        # A turn with no file changes is honest about it.
        ws.record_turn(root, seq=1, provider="agy", summary="answered a question", lane="chat", files=None)
        brief = ws.read_brief(root)
        assert "**turn 1** · agy · chat" in brief
        assert "(no file changes)" in brief

    @pytest.mark.asyncio
    async def test_activity_log_is_bounded(self, tmp_path, monkeypatch):
        """The brief feeds every prompt + CLI launch file, so the per-turn Activity
        log is capped to a recent tail — old entries drop, a trim marker appears."""
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        root = await ws.create_workspace("led2", title="T", goal="G")

        total = ws.ACTIVITY_MAX_ENTRIES + 15
        for i in range(total):
            ws.record_turn(root, seq=i, provider="claude", summary=f"turn {i} work", lane="chat")

        brief = ws.read_brief(root)
        kept = [ln for ln in brief.splitlines() if ln.lstrip().startswith("- **turn")]
        assert len(kept) == ws.ACTIVITY_MAX_ENTRIES        # bounded, not `total`
        assert ws.ACTIVITY_TRIM_MARKER in brief            # trim is disclosed
        assert f"**turn {total - 1}**" in brief            # newest kept
        assert "**turn 0**" not in brief                   # oldest dropped

    @pytest.mark.asyncio
    async def test_summary_block_upsert_and_read(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        root = await ws.create_workspace("led2", title="T", goal="G")

        assert ws.read_summary(root) == ""                      # placeholder → empty
        ws.set_summary(root, "Goal is a landing page. Hero + pricing done; nav pending.")
        assert "Hero + pricing done" in ws.read_summary(root)
        # Idempotent upsert — single managed block, Activity section intact.
        ws.record_turn(root, seq=0, provider="mock", summary="x", lane="chat")
        ws.set_summary(root, "Updated: nav added.")
        brief = ws.read_brief(root)
        assert brief.count(ws.SUMMARY_BEGIN) == 1
        assert ws.read_summary(root) == "Updated: nav added."
        assert "**turn 0** · mock" in brief                     # activity preserved

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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        with pytest.raises(ValueError):
            ws.workspace_root("../../etc")

    @pytest.mark.asyncio
    async def test_turn_context_uses_workspace_not_transcript(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        root = await ws.create_workspace("sess2", title="T", goal="G")
        ws.record_turn(root, seq=0, provider="grok", summary="built hero section", lane="chat")

        ctx = ws.build_turn_context(root, "make the hero bigger")
        assert "built hero section" in ctx     # from SESSION.md brief
        assert "SESSION.md" in ctx              # discovery pointer names the brief file
        assert "make the hero bigger" in ctx    # the new user message
        # Standardised Python-dependency workflow: install into `.venv`, not a stray
        # `packages/` dir (the agy behaviour this guidance corrects).
        assert "Installing Python packages" in ctx and ".venv" in ctx

    async def test_turn_context_includes_recent_dialogue(self, tmp_path, monkeypatch):
        # A short follow-up keeps its referent via the replayed dialogue tail.
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        root = await ws.create_workspace("sess3", title="T", goal="G")

        ctx = ws.build_turn_context(
            root, "yes do that",
            recent_turns=[("count LOC", "Total LOC: 138. Want me to count all text files?")],
        )
        assert "Recent conversation" in ctx
        assert "count all text files" in ctx   # the assistant's prior proposal
        assert "yes do that" in ctx            # the follow-up
        # Without recent_turns the tail is omitted (workspace stays primary).
        assert "Recent conversation" not in ws.build_turn_context(root, "hi")


# ── Session turn lifecycle (Verify gate) ──────────────────────────────────────

@pytest.fixture
async def session_env(tmp_path, monkeypatch):
    """Fresh DB + patched session orchestrator/workspace pointing at tmp_path."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner  # register metadata

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
    monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False)

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

        from sqlalchemy import select

        from app.models import Session as SessionModel
        from app.models import SessionTurn
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
        assert "**turn 0** · mock" in brief and "**turn 1** · grok-mock" in brief
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


class TestTurnInterrupt:
    """P-0057/D-0051: best-effort interrupt of an in-flight turn."""

    @pytest.mark.asyncio
    async def test_cancel_marks_turn_cancelled(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        # A slow executor so the turn is still streaming when we interrupt it.
        orch.get_executor = lambda name: MockExecutor(name=name, latency_ms=3000)

        from app.models import SessionTurn
        turn_id, _ = await orch.create_turn_record(
            "s1", "build something large", owner_id="local"
        )
        task = orch.dispatch_turn(turn_id, "s1", owner_id="local")
        await asyncio.sleep(0.1)  # let the stream start

        signalled = await orch.cancel_turn(turn_id, "s1", owner_id="local")
        assert signalled is True

        # The background task ends cancelled.
        with pytest.raises(asyncio.CancelledError):
            await task

        async with Maker() as db:
            turn = await db.get(SessionTurn, turn_id)
            assert turn.status == "cancelled"
            assert turn.finished_at is not None

        # A cancelled turn-update was broadcast, and the handle was cleaned up.
        assert any(
            b.get("turn", {}).get("status") == "cancelled" for b in broadcasts
        )
        assert turn_id not in orch._turn_cancel_handles

    @pytest.mark.asyncio
    async def test_reap_orphaned_turns_marks_running_failed(self, session_env):
        """A turn left 'running' by a restart is reaped to 'failed' at startup so the
        chat shows honest state (no phantom Stop button)."""
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        from app.models import SessionTurn
        async with Maker() as db:
            db.add(SessionTurn(session_id="s1", owner_id="local", seq=0,
                               provider="mock", prompt="stranded", status="running"))
            db.add(SessionTurn(session_id="s1", owner_id="local", seq=1,
                               provider="mock", prompt="done", status="succeeded"))
            await db.commit()

        reaped = await orch.reap_orphaned_turns()
        assert reaped == 1

        async with Maker() as db:
            from sqlalchemy import select
            rows = (await db.execute(
                select(SessionTurn).where(SessionTurn.session_id == "s1").order_by(SessionTurn.seq)
            )).scalars().all()
            assert rows[0].status == "failed"
            assert "restart" in (rows[0].error or "")
            assert rows[0].finished_at is not None
            assert rows[1].status == "succeeded"  # terminal turns untouched

    @pytest.mark.asyncio
    async def test_cancel_unknown_or_finished_turn_returns_false(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        # No in-flight task registered for this id → nothing to signal.
        signalled = await orch.cancel_turn(424242, "s1", owner_id="local")
        assert signalled is False

    @pytest.mark.asyncio
    async def test_continue_after_cancel_seeds_next_turn(self, session_env):
        """B (cross-provider continue): after an interrupt, the next message is a
        normal turn that succeeds — continuation is prompt-driven and provider-
        agnostic (history + workspace replay), not a special resume."""
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        orch.get_executor = lambda name: MockExecutor(name=name, latency_ms=3000)

        from app.models import SessionTurn
        turn_id, _ = await orch.create_turn_record("s1", "start a big build", owner_id="local")
        task = orch.dispatch_turn(turn_id, "s1", owner_id="local")
        await asyncio.sleep(0.1)
        await orch.cancel_turn(turn_id, "s1", owner_id="local")
        with pytest.raises(asyncio.CancelledError):
            await task

        # Continue on a DIFFERENT provider with a fast executor — a plain next turn.
        orch.get_executor = lambda name: MockExecutor(name=name, latency_ms=1)
        next_id = await orch.run_turn(
            "s1", "continue where you left off", provider="grok-mock", owner_id="local"
        )
        async with Maker() as db:
            nxt = await db.get(SessionTurn, next_id)
            assert nxt.status == "succeeded"
            assert nxt.provider == "grok-mock"
            assert nxt.seq > 0  # appended after the cancelled turn


class TestCancelAndTimeoutSnapshot:
    """P-0069 items 2+3+4 + 4b: an interrupted turn (user cancel or session-turn
    timeout) snapshots its partial workspace edits before teardown, records a
    kill-event, and (project-scoped) captures partial-diff evidence."""

    @pytest.mark.asyncio
    async def test_cancel_snapshots_partial_work(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        orch.get_executor = lambda name: _WriteThenHangExecutor(name=name)

        from app.models import SessionTurn
        turn_id, _ = await orch.create_turn_record("s1", "build a page", owner_id="local")
        task = orch.dispatch_turn(turn_id, "s1", owner_id="local")
        assert await _wait_for_file_write(broadcasts), "executor never wrote the file"

        assert await orch.cancel_turn(turn_id, "s1", owner_id="local") is True
        with pytest.raises(asyncio.CancelledError):
            await task

        # The partial edits were committed as a version and stamped on the turn.
        async with Maker() as db:
            turn = await db.get(SessionTurn, turn_id)
            assert turn.status == "cancelled"
            assert turn.commit_sha is not None
            assert turn.changed_files and "index.html" in turn.changed_files

        # A kill-event carrying the forensics was broadcast.
        kills = [b for b in broadcasts if (b.get("event") or {}).get("phase") == "kill_event"]
        assert kills, "no kill_event broadcast"
        data = kills[-1]["event"]["data"]
        assert data["reason"] == "cancelled"
        assert data["commit"] is not None
        assert any(
            (f.get("path") if isinstance(f, dict) else f) == "index.html"
            for f in data["files"]
        )
        assert data["last_file_mtime"] is not None

    @pytest.mark.asyncio
    async def test_timeout_fails_loudly_and_snapshots(self, session_env, monkeypatch):
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        orch.get_executor = lambda name: _WriteThenHangExecutor(name=name)
        # Tighten the effective policy timeout (session default = run_timeout_seconds,
        # the lru-cached settings singleton policy.py also reads).
        monkeypatch.setattr(orch._settings, "run_timeout_seconds", 0.3, raising=False)

        from app.models import SessionTurn
        turn_id, _ = await orch.create_turn_record("s1", "do a long thing", owner_id="local")
        task = orch.dispatch_turn(turn_id, "s1", owner_id="local")
        # Timeout path returns normally (not cancelled) once the deadline fires.
        await task

        async with Maker() as db:
            turn = await db.get(SessionTurn, turn_id)
            assert turn.status == "failed"
            assert "timed out" in (turn.error or "")
            assert turn.commit_sha is not None            # partial work still captured
            assert turn.changed_files and "index.html" in turn.changed_files

        kills = [b for b in broadcasts if (b.get("event") or {}).get("phase") == "kill_event"]
        assert kills and kills[-1]["event"]["data"]["reason"] == "timeout"
        # The turn update surfaces the timeout distinctly from a plain failure.
        assert any(
            (b.get("turn") or {}).get("status") == "failed" and b["turn"].get("timeout")
            for b in broadcasts
        )

    @pytest.mark.asyncio
    async def test_cancel_captures_partial_diff_evidence(self, session_env, monkeypatch, tmp_path):
        Maker, ws, orch, broadcasts = session_env
        orch.get_executor = lambda name: _WriteThenHangExecutor(name=name)
        from app import evidence as evidence_store
        monkeypatch.setattr(
            evidence_store._settings, "evidence_dir", str(tmp_path / "evidence"), raising=False
        )

        # A project-scoped session so the diff is indexed as evidence.
        from app.models import Evidence, Project, Session as SessionModel
        root = await ws.create_workspace("sp", title="P", goal="G")
        async with Maker() as db:
            db.add(Project(id="p1", owner_id="local", name="P1"))
            db.add(SessionModel(
                id="sp", owner_id="local", title="P", provider="mock",
                workspace_path=root, status="active", project_id="p1",
            ))
            await db.commit()

        turn_id, _ = await orch.create_turn_record("sp", "build", owner_id="local")
        task = orch.dispatch_turn(turn_id, "sp", owner_id="local")
        assert await _wait_for_file_write(broadcasts)
        await orch.cancel_turn(turn_id, "sp", owner_id="local")
        with pytest.raises(asyncio.CancelledError):
            await task

        from sqlalchemy import select
        async with Maker() as db:
            ev = (await db.execute(
                select(Evidence).where(Evidence.session_turn_id == turn_id)
            )).scalars().all()
            assert len(ev) == 1
            assert ev[0].kind == "partial-diff"
            assert ev[0].project_id == "p1"


# ── Rename endpoint (HTTP) ────────────────────────────────────────────────────

class TestSessionRename:
    """PATCH /api/sessions/{id} renames the session; empty/whitespace is rejected."""

    def test_patch_renames_and_validates(self, tmp_path):
        import asyncio

        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base, get_db
        from app.main import _owner_id, app
        from app.models import Owner
        from app.models import Session as SessionModel

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


class TestSessionBudget:
    """PATCH sets/raises/clears the per-session budget; GET surfaces cumulative cost."""

    def test_budget_set_clear_and_cost_surfacing(self, tmp_path):
        import asyncio

        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base, get_db
        from app.main import _owner_id, app
        from app.models import Owner
        from app.models import Session as SessionModel
        from app.models import SessionTurn

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.get_event_loop().run_until_complete(_setup())
        Maker = async_sessionmaker(engine, expire_on_commit=False)

        async def _seed():
            async with Maker() as db:
                db.add(Owner(id="local", label="Test"))
                db.add(SessionModel(
                    id="s1", owner_id="local", title="S", provider="mock",
                    workspace_path=str(tmp_path), preview_token="tok", status="active",
                ))
                # Two succeeded turns → cumulative cost = 0.30.
                db.add(SessionTurn(session_id="s1", owner_id="local", seq=1,
                                   prompt="a", status="succeeded", cost_usd=0.10))
                db.add(SessionTurn(session_id="s1", owner_id="local", seq=2,
                                   prompt="b", status="succeeded", cost_usd=0.20))
                await db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)
            # GET surfaces cumulative spend; budget unset by default.
            g = c.get("/api/sessions/s1").json()
            assert g["cost_usd"] == 0.30
            assert g["budget_usd"] is None
            # Set a cap.
            r = c.patch("/api/sessions/s1", json={"budget_usd": 5.0})
            assert r.status_code == 200 and r.json()["budget_usd"] == 5.0
            # Raise it.
            assert c.patch("/api/sessions/s1", json={"budget_usd": 10.0}).json()["budget_usd"] == 10.0
            # 0 clears back to no cap.
            assert c.patch("/api/sessions/s1", json={"budget_usd": 0}).json()["budget_usd"] is None
            # Negative rejected.
            assert c.patch("/api/sessions/s1", json={"budget_usd": -1}).status_code == 422
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())


class TestSessionDelete:
    """DELETE /api/sessions/{id} removes the row + its workspace dir; 404 otherwise."""

    def test_delete_removes_row_and_workspace(self, tmp_path):
        import asyncio
        import os

        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base, get_db
        from app.main import _owner_id, app
        from app.models import Owner
        from app.models import Session as SessionModel

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/d.db", echo=False)
        ws_dir = tmp_path / "ws_s1"
        ws_dir.mkdir()
        (ws_dir / "index.html").write_text("<h1>hi</h1>")

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
                    workspace_path=str(ws_dir), preview_token="tok", status="active",
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
            assert c.delete("/api/sessions/s1").status_code == 204
            # Row is gone and the workspace directory was torn down.
            assert c.get("/api/sessions/s1").status_code == 404
            assert not os.path.isdir(ws_dir)
            # Idempotent on a second delete; unknown id is 404 too.
            assert c.delete("/api/sessions/s1").status_code == 404
            assert c.delete("/api/sessions/nope").status_code == 404
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())


class TestSessionListContentSignals:
    """GET /api/sessions surfaces turn_count + published so the UI can scale the
    delete confirmation (empty → quick confirm; content/published → type-to-confirm)."""

    def test_list_reports_turn_count_and_published(self, tmp_path):
        import asyncio

        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base, get_db
        from app.main import _owner_id, app
        from app.models import Artifact, Owner, SessionTurn
        from app.models import Session as SessionModel

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.get_event_loop().run_until_complete(_setup())
        Maker = async_sessionmaker(engine, expire_on_commit=False)

        async def _seed():
            async with Maker() as db:
                db.add(Owner(id="local", label="Test"))
                # empty session, session with 2 turns, and a published session.
                for sid in ("empty", "withturns", "pub"):
                    db.add(SessionModel(
                        id=sid, owner_id="local", title=sid, provider="mock",
                        workspace_path=str(tmp_path / sid), preview_token=sid, status="active",
                    ))
                db.add(SessionTurn(session_id="withturns", owner_id="local", seq=1, prompt="a"))
                db.add(SessionTurn(session_id="withturns", owner_id="local", seq=2, prompt="b"))
                db.add(Artifact(session_id="pub", owner_id="local", published=True, share_token="tok"))
                await db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)
            by_id = {s["id"]: s for s in c.get("/api/sessions").json()}
            assert by_id["empty"]["turn_count"] == 0 and by_id["empty"]["published"] is False
            assert by_id["withturns"]["turn_count"] == 2 and by_id["withturns"]["published"] is False
            assert by_id["pub"]["published"] is True
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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
        root = await ws.create_workspace("v2", title="T", goal="G")
        # No file edits after init → no new version.
        assert await ws.commit_turn(root, seq=0, provider="mock") is None

    @pytest.mark.asyncio
    async def test_list_versions_newest_first(self, tmp_path, monkeypatch):
        from app.sessions import workspace as ws
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
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
        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path), raising=False)
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


class TestTerminalCapture:
    """D-0017 thread 2: web-TTY terminal lane artifact capture."""

    @pytest.mark.asyncio
    async def test_capture_records_terminal_edits_as_artifact_turn(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        root = await _make_session(Maker, ws)

        # Simulate the human-driven CLI editing the workspace (no commit boundary).
        with open(os.path.join(root, "notes.md"), "w") as f:
            f.write("# from terminal\n")

        turn_id = await orch.capture_terminal_snapshot(
            "s1", provider="claude", owner_id="local"
        )
        assert turn_id is not None

        import json

        from app.models import SessionTurn
        async with Maker() as db:
            turn = await db.get(SessionTurn, turn_id)
            assert turn.status == "succeeded"
            assert turn.provider == "claude"
            assert turn.commit_sha and len(turn.commit_sha) == 40
            files = json.loads(turn.changed_files)
            assert any(f["path"] == "notes.md" for f in files)

        # The capture is a real version in workspace history.
        versions = await ws.list_versions(root)
        assert any("terminal session (claude)" in v["message"] for v in versions)

    @pytest.mark.asyncio
    async def test_capture_noop_when_workspace_unchanged(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        await _make_session(Maker, ws)
        # No terminal edits → no turn recorded.
        assert await orch.capture_terminal_snapshot("s1", owner_id="local") is None

    @pytest.mark.asyncio
    async def test_capture_unknown_session_raises(self, session_env):
        Maker, ws, orch, broadcasts = session_env
        with pytest.raises(orch.SessionError):
            await orch.capture_terminal_snapshot("nope", owner_id="local")


class TestLedgerSummary:
    """D-0017 thread 1 slice 2: optional LLM summarizer + sovereignty rule."""

    def test_pick_summarizer_confidential_is_local_only(self, monkeypatch):
        from app.sessions import ledger
        # A configured remote summarizer must NOT be chosen for a confidential session.
        monkeypatch.setattr(ledger._settings, "ledger_summary_provider", "claude", raising=False)
        monkeypatch.setattr(ledger, "local_candidate_ids", lambda: ["ollama"])
        monkeypatch.setattr(ledger, "get_executor", lambda cid: object())
        assert ledger._pick_summarizer("claude", confidential=True) == "ollama"
        # No local available → skip entirely (fail closed, deterministic ledger stands).
        monkeypatch.setattr(ledger, "local_candidate_ids", lambda: [])
        assert ledger._pick_summarizer("claude", confidential=True) is None

    @pytest.mark.asyncio
    async def test_summarize_force_writes_summary(self, session_env, monkeypatch):
        Maker, ws, orch, _ = session_env
        await _make_session(Maker, ws)
        from app.sessions import ledger
        monkeypatch.setattr(ledger, "AsyncSessionLocal", Maker)
        monkeypatch.setattr(ledger, "get_executor", lambda name: MockExecutor(name=name, latency_ms=1))

        # Disabled + not forced → no-op.
        monkeypatch.setattr(ledger._settings, "ledger_summary_enabled", False, raising=False)
        assert await ledger.summarize_session("s1", force=False) is None
        assert ws.read_summary(await _ws_root(Maker, "s1")) == ""

        # Forced → summarizes via the (mock) model and writes the block.
        text = await ledger.summarize_session("s1", force=True)
        assert text
        assert ws.read_summary(await _ws_root(Maker, "s1")) == text

    @pytest.mark.asyncio
    async def test_summarize_confidential_skips_without_local(self, session_env, monkeypatch):
        Maker, ws, orch, _ = session_env
        root = await _make_session(Maker, ws, session_id="sc")
        from app.models import Session as SessionModel
        async with Maker() as db:
            s = await db.get(SessionModel, "sc")
            s.confidential = True
            await db.commit()
        from app.sessions import ledger
        monkeypatch.setattr(ledger, "AsyncSessionLocal", Maker)
        monkeypatch.setattr(ledger, "local_candidate_ids", lambda: [])  # no local model
        monkeypatch.setattr(ledger, "get_executor", lambda name: MockExecutor(name=name))
        # Confidential + no local → skip; nothing sent to a remote model.
        assert await ledger.summarize_session("sc", force=True) is None
        assert ws.read_summary(root) == ""


async def _ws_root(Maker, session_id):
    from app.models import Session as SessionModel
    async with Maker() as db:
        return (await db.get(SessionModel, session_id)).workspace_path


class TestVersioningHTTP:
    """versions / diff / restore endpoints + owner_id isolation (M1.3 gate)."""

    def test_versions_diff_restore_and_owner_isolation(self, tmp_path, monkeypatch):
        import asyncio

        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base, get_db
        from app.main import _owner_id, app
        from app.models import Owner
        from app.models import Session as SessionModel
        from app.sessions import workspace as ws

        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False)
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
            asyncio.get_event_loop().run_until_complete(engine.dispose())
