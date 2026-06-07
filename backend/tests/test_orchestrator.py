"""
tests/test_orchestrator.py — P6 gate: full run lifecycle on mock provider.

Tests:
- Task → enqueue → run succeeds with mock executor
- Failover: first candidate rate-limited → second candidate succeeds
- All candidates cooling → status=deferred with deferred_until
- WS events emitted during run (run.update, run.event)
- Output files written (markdown_path)
"""
from __future__ import annotations

import asyncio
import pytest
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from app.providers.base import EventKind, ExecEvent, ExecResult, Usage
from app.providers.mock import MockExecutor
from app.quota import QuotaTracker


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_result(text: str = "Mock report content.", provider: str = "mock") -> ExecResult:
    return ExecResult(
        text=text,
        usage=Usage(tokens_in=100, tokens_out=200, cost_usd=0.0),
        provider=provider,
        model="mock-v1",
    )


async def _drain_mock(ex: MockExecutor, prompt: str = "Test") -> list[ExecEvent]:
    events = []
    async for ev in ex.run_stream(prompt, workdir="/tmp", tools_enabled=False):
        events.append(ev)
    return events


# ── Mock executor unit tests ──────────────────────────────────────────────────

class TestMockExecutorEvents:
    @pytest.mark.asyncio
    async def test_healthy_mock_produces_result(self):
        ex = MockExecutor(latency_ms=1)
        events = await _drain_mock(ex)
        kinds = [e.kind for e in events]
        assert EventKind.result in kinds
        result_ev = next(e for e in events if e.kind == EventKind.result)
        assert result_ev.data["result"].provider == "mock"

    @pytest.mark.asyncio
    async def test_mock_streams_tokens(self):
        ex = MockExecutor(latency_ms=1, token_chunks=3)
        events = await _drain_mock(ex)
        token_events = [e for e in events if e.kind == EventKind.token]
        assert len(token_events) > 0
        full = "".join(e.text or "" for e in token_events)
        assert len(full) > 50

    @pytest.mark.asyncio
    async def test_simulated_rate_limit_yields_error(self):
        ex = MockExecutor(latency_ms=1, simulate_rate_limit=True)
        events = await _drain_mock(ex)
        kinds = [e.kind for e in events]
        assert EventKind.error in kinds
        assert EventKind.result not in kinds
        err = next(e for e in events if e.kind == EventKind.error)
        assert err.data.get("rate_limit") is True

    @pytest.mark.asyncio
    async def test_event_sequence_correct(self):
        ex = MockExecutor(latency_ms=1, token_chunks=2)
        events = await _drain_mock(ex)
        kinds = [e.kind for e in events]
        # Must start with log, then phase events, then tokens, then result
        assert kinds[0] == EventKind.log
        assert EventKind.phase in kinds
        assert kinds[-1] == EventKind.result

    def test_is_healthy_normal(self):
        assert MockExecutor().is_healthy() is True

    def test_is_healthy_rate_limited(self):
        ex = MockExecutor(simulate_rate_limit=True)
        assert ex.is_healthy() is False


# ── ExecEvent terminal check ──────────────────────────────────────────────────

class TestExecEventTerminal:
    def test_result_is_terminal(self):
        ev = ExecEvent(kind=EventKind.result, message="done")
        assert ev.is_terminal() is True

    def test_error_is_terminal(self):
        ev = ExecEvent(kind=EventKind.error, message="oops")
        assert ev.is_terminal() is True

    def test_token_not_terminal(self):
        ev = ExecEvent(kind=EventKind.token, text="hello")
        assert ev.is_terminal() is False

    def test_log_not_terminal(self):
        ev = ExecEvent(kind=EventKind.log, message="info")
        assert ev.is_terminal() is False


# ── JSON-block stripping / empty-heading cleanup ──────────────────────────────

class TestStripJsonBlock:
    def test_orphaned_heading_removed(self):
        """The heading that introduced an extracted json block must not linger."""
        from app.orchestrator import _strip_json_block
        text = (
            "# Daily Macro Market Brief\n\n## Summary\nMarkets were mixed.\n\n"
            "## Structured Data (JSON)\n\n```json\n{\"sp500\": 1.2}\n```\n"
        )
        out = _strip_json_block(text)
        assert "Structured Data" not in out
        assert "Markets were mixed." in out
        assert not out.rstrip().endswith(")")  # no dangling heading at end

    def test_parent_with_content_children_preserved(self):
        from app.orchestrator import _strip_json_block
        text = (
            "# Report\n\n## Section A\nreal content\n\n## Data\n```json\n{\"x\":1}\n```\n"
        )
        out = _strip_json_block(text)
        assert "# Report" in out and "## Section A" in out and "real content" in out
        assert "## Data" not in out

    def test_heading_with_trailing_prose_kept(self):
        from app.orchestrator import _strip_json_block
        text = "## Data\n```json\n{\"x\":1}\n```\nNotes: see above.\n"
        out = _strip_json_block(text)
        assert "## Data" in out and "Notes: see above." in out

    def test_trailing_separator_trimmed_but_inner_kept(self):
        from app.orchestrator import _strip_json_block
        text = (
            "# R\n\n## A\nfoo\n\n---\n\n## B\nbar\n\n"
            "## Data\n```json\n{}\n```\n"
        )
        out = _strip_json_block(text)
        assert out.endswith("bar")          # no dangling trailing separator
        assert "---" in out                 # inner separator between A and B preserved
        assert "## Data" not in out

    def test_no_json_block_is_noop(self):
        from app.orchestrator import _strip_json_block
        assert _strip_json_block("# Title\n\nbody") == "# Title\n\nbody"


# ── Router + failover integration (no DB) ────────────────────────────────────

class TestFailoverLogic:
    """Test the failover loop logic without a real DB using the quota tracker."""

    def test_rate_limited_provider_marked_unhealthy(self):
        q = QuotaTracker()
        assert q.is_healthy("mock") is True
        q.mark_cooldown("mock", datetime.now(timezone.utc) + timedelta(minutes=5))
        assert q.is_healthy("mock") is False

    def test_cooldown_provider_skipped_by_router(self):
        from app.router import resolve, CandidatePlan, DeferredResult
        q = QuotaTracker()
        q.mark_cooldown("mock", datetime.now(timezone.utc) + timedelta(minutes=5))

        result = resolve({"strategy": "capability", "candidates": ["mock"],
                          "capability_tags": [], "failover": True, "max_attempts": 3}, q)
        assert isinstance(result, DeferredResult)

    def test_second_candidate_used_after_first_cooling(self):
        from app.router import resolve, CandidatePlan
        q = QuotaTracker()
        q.mark_cooldown("mock", datetime.now(timezone.utc) + timedelta(minutes=5))
        # open-default is in registry and healthy
        result = resolve({"strategy": "capability",
                          "candidates": ["mock", "open-default"],
                          "capability_tags": [], "failover": True, "max_attempts": 3}, q)
        assert isinstance(result, CandidatePlan)
        assert "mock" not in result.candidates
        assert "open-default" in result.candidates

    def test_all_cooling_deferred_with_earliest_reset(self):
        from app.router import resolve, DeferredResult
        q = QuotaTracker()
        reset1 = datetime.now(timezone.utc) + timedelta(minutes=10)
        reset2 = datetime.now(timezone.utc) + timedelta(minutes=5)
        q.mark_cooldown("mock", reset1)
        q.mark_cooldown("open-default", reset2)
        result = resolve({"strategy": "capability",
                          "candidates": ["mock", "open-default"],
                          "capability_tags": [], "failover": True, "max_attempts": 3},
                         q, deployment_mode="personal")
        assert isinstance(result, DeferredResult)
        assert result.deferred_until is not None
        # Should be the minimum of the two resets (reset2 for open-default)
        diff = abs((result.deferred_until - reset2).total_seconds())
        assert diff < 2


# ── D-0016 cron headless-capability filter ──────────────────────────────────

class TestHeadlessCapability:
    """D-0016/P-0019: scheduled tasks ride the headless `cli -p` lane; providers
    without a headless mode are filtered from scheduled rotation. As of 2026-06-06
    all four plan CLIs (incl. grok `-p/--single`) have first-party headless modes,
    so the no-headless set is empty — but the mechanism stays for future providers."""

    def test_all_plan_clis_are_headless_capable(self):
        from app.providers.registry import is_headless_capable
        # grok `-p/--single` is first-party (verified live 2026-06-06).
        for p in ("claude", "codex", "agy", "grok"):
            assert is_headless_capable(p) is True

    def test_capability_checks_template_not_instance(self):
        # Multi-account instance ids inherit their template's capability.
        from app.providers.registry import is_headless_capable
        assert is_headless_capable("grok:work") is True
        assert is_headless_capable("claude:personal") is True

    def test_non_cli_candidates_are_capable(self):
        # mock / api / local providers aren't plan-CLIs → unaffected.
        from app.providers.registry import is_headless_capable
        for p in ("mock", "claude-api", "ollama", "openai-api"):
            assert is_headless_capable(p) is True

    def test_filter_mechanism_works_for_a_hypothetical_no_headless_provider(self, monkeypatch):
        # Guard the mechanism itself even though the set is currently empty:
        # if a future provider lacks `-p`, it must be reported incapable.
        import app.providers.registry as reg
        monkeypatch.setattr(reg, "_NO_HEADLESS_CLI_TEMPLATES", frozenset({"futurecli"}))
        assert reg.is_headless_capable("futurecli") is False
        assert reg.is_headless_capable("futurecli:acct") is False
        assert reg.is_headless_capable("grok") is True


# ── Orchestrator smoke test (in-process with mock, patched DB) ───────────────

@pytest.fixture
async def fresh_db(tmp_path):
    """Provide a fresh SQLite DB + AsyncSessionLocal for each test."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    # Import Base via models (already loaded) not directly from db
    # to avoid triggering module-level engine creation in db.py
    from app.models import Owner, Task, Run, RunEvent  # ensure metadata registered
    from app.db import Base  # safe now; module already imported, engine already created

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


class TestOrchestratorSmoke:
    """Full end-to-end smoke test using mock provider and in-memory SQLite."""

    @pytest.mark.asyncio
    async def test_run_succeeds_via_mock(self, fresh_db, tmp_path):
        engine, Session, base_path = fresh_db

        # Patch DB session and outputs_dir
        import app.orchestrator as orch_mod

        # orchestrator uses `from app.db import AsyncSessionLocal` so we patch the name
        # in the orchestrator module itself, not in app.db
        orig_session_local = orch_mod.AsyncSessionLocal

        orch_mod.AsyncSessionLocal = Session

        outputs = str(base_path / "outputs")
        orch_mod._settings.__dict__["outputs_dir"] = outputs  # bypass frozen pydantic
        orch_mod._settings.__dict__["work_dir"] = str(base_path / "work")  # P-0022 task workspaces

        try:
            from app.models import Task
            async with Session() as db:
                task = Task(
                    owner_id="local",
                    name="Test task",
                    prompt_template="Tell me about {topic}",
                    params={"topic": "AI"},
                    routing={
                        "strategy": "capability",
                        "candidates": ["mock"],
                        "capability_tags": [],
                        "failover": True,
                        "max_attempts": 1,
                    },
                    want_markdown=True,
                    want_json=False,
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                task_id = task.id

            run = await orch_mod.enqueue_run(task_id, trigger="test")
            run_id = run.id

            bg = orch_mod._cancel_handles.get(run_id)
            if bg:
                try:
                    await asyncio.wait_for(asyncio.shield(bg), timeout=8.0)
                except asyncio.TimeoutError:
                    pass

            await asyncio.sleep(0.3)

            from app.models import Run
            async with Session() as db:
                run = await db.get(Run, run_id)
                assert run is not None, "Run not found"
                assert run.status == "succeeded", f"status={run.status} error={run.error}"
                assert run.provider == "mock"
                assert run.tokens_out == 200
                assert run.markdown_path is not None
                import os
                assert os.path.exists(run.markdown_path)

        finally:
            orch_mod.AsyncSessionLocal = orig_session_local
            if "outputs_dir" in orch_mod._settings.__dict__:
                del orch_mod._settings.__dict__["outputs_dir"]
            if "work_dir" in orch_mod._settings.__dict__:
                del orch_mod._settings.__dict__["work_dir"]

    @pytest.mark.asyncio
    async def test_scheduled_run_filters_no_headless_candidate(self, fresh_db, tmp_path, monkeypatch):
        """D-0016: a scheduled run drops a no-headless candidate from rotation and
        proceeds on a headless-capable provider, emitting a cron_no_headless_filter
        route event. All real plan CLIs now have headless modes, so we patch the
        no-headless set to a hypothetical template to exercise the filter path."""
        engine, Session, base_path = fresh_db
        import app.orchestrator as orch_mod
        import app.providers.registry as reg
        from app.config import DeploymentMode

        # monkeypatch.setitem auto-restores frozen-Settings fields (vs. del, which
        # would remove the field entirely and break later tests).
        monkeypatch.setattr(orch_mod, "AsyncSessionLocal", Session)
        monkeypatch.setitem(orch_mod._settings.__dict__, "outputs_dir", str(base_path / "outputs"))
        monkeypatch.setitem(orch_mod._settings.__dict__, "work_dir", str(base_path / "work"))
        monkeypatch.setitem(orch_mod._settings.__dict__, "cron_allow_no_headless_providers", False)
        monkeypatch.setitem(orch_mod._settings.__dict__, "deployment_mode", DeploymentMode.personal)
        # No real CLI lacks headless anymore, so simulate one to test the filter.
        monkeypatch.setattr(reg, "_NO_HEADLESS_CLI_TEMPLATES", frozenset({"nohead"}))

        from app.models import Task, Run, RunEvent
        from sqlalchemy import select
        async with Session() as db:
            task = Task(
                owner_id="local",
                name="Scheduled task",
                prompt_template="Tell me about {topic}",
                params={"topic": "AI"},
                schedule_kind="cron",
                schedule_expr="0 7 * * *",
                routing={
                    "strategy": "fixed",
                    "candidates": ["nohead", "mock"],  # nohead has no headless mode
                    "failover": True,
                    "max_attempts": 2,
                },
                want_markdown=True,
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

        # trigger="schedule" is what activates the cron filter.
        run = await orch_mod.enqueue_run(task_id, trigger="schedule")
        run_id = run.id
        bg = orch_mod._cancel_handles.get(run_id)
        if bg:
            try:
                await asyncio.wait_for(asyncio.shield(bg), timeout=8.0)
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(0.3)

        async with Session() as db:
            run = await db.get(Run, run_id)
            assert run is not None
            # Filtered to mock → succeeds on the headless-capable provider.
            assert run.status == "succeeded", f"status={run.status} error={run.error}"
            assert run.provider == "mock"

            events = (await db.execute(
                select(RunEvent).where(RunEvent.run_id == run_id)
            )).scalars().all()
            filt = [e for e in events if e.phase == "cron_no_headless_filter"]
            assert filt, "expected a cron_no_headless_filter route event"
            assert filt[0].data["dropped"] == ["nohead"]

    @pytest.mark.asyncio
    async def test_run_deferred_when_all_cooling(self, fresh_db, tmp_path):
        engine, Session, base_path = fresh_db

        import app.orchestrator as orch_mod
        from app.quota import quota_tracker

        # Patch directly on orchestrator module (from-import binding)
        orig_session_local = orch_mod.AsyncSessionLocal
        orch_mod.AsyncSessionLocal = Session
        orch_mod._settings.__dict__["outputs_dir"] = str(base_path / "outputs")

        # Pre-cool mock
        quota_tracker.mark_cooldown("mock", datetime.now(timezone.utc) + timedelta(minutes=5))

        try:
            from app.models import Task
            async with Session() as db:
                task = Task(
                    owner_id="local",
                    name="Deferred task",
                    prompt_template="Test",
                    routing={
                        "strategy": "capability",
                        "candidates": ["mock"],
                        "capability_tags": [],
                        "failover": True,
                        "max_attempts": 1,
                    },
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                task_id = task.id

            run = await orch_mod.enqueue_run(task_id, trigger="test")
            run_id = run.id

            bg = orch_mod._cancel_handles.get(run_id)
            if bg:
                try:
                    await asyncio.wait_for(asyncio.shield(bg), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

            await asyncio.sleep(0.2)

            from app.models import Run
            async with Session() as db:
                run = await db.get(Run, run_id)
                assert run is not None
                assert run.status == "deferred"
                assert run.deferred_until is not None

        finally:
            orch_mod.AsyncSessionLocal = orig_session_local
            if "outputs_dir" in orch_mod._settings.__dict__:
                del orch_mod._settings.__dict__["outputs_dir"]
            quota_tracker.mark_healthy("mock")
