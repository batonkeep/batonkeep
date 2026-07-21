"""
tests/test_planner.py — the per-project planner agent lane (P-0078, slice 1).

Covers the three seams the planner reuses without becoming a new engine:
  - selection resolution (fallback + the confidential→local sovereignty fence);
  - the planning-mode toolset/prompt gating in the model executor;
  - the two proposer-only tools (propose_subtasks / set_next_action) landing on a
    WorkItem via the registry, driven end-to-end through run_planning_turn; and
  - the HTTP lane (POST /plan → 202 running, poll GET).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_db
from app.main import _owner_id, app
from app.models import Owner, PlannerRun, Project, WorkItem
from app.providers.base import EventKind, ExecEvent, ExecResult, Executor, Usage
from app.providers.tools.registry import get_tool_registry


class _PlanningExecutor(Executor):
    """A fake planner: in planning mode it dispatches the planning tools through the
    registry using the run `context` (proving the executor→tool→WorkItem wiring),
    then returns a result. Mirrors what a real model would do via tool-calling."""

    name = "planner-mock"
    tier = "mock"

    def __init__(self, name: str = "planner-mock", *, items=None, next_action="ship it") -> None:
        self.name = name
        self.tier = "mock"
        self._items = items if items is not None else [
            {"label": "the report", "expected": "report.md"},
            {"label": "review notes"},
        ]
        self._next_action = next_action

    @property
    def kind(self) -> str:
        return "mock"

    def is_healthy(self) -> bool:
        return True

    async def run_stream(self, prompt, *, workdir, tools_enabled=True,
                         max_rounds=10, budget_usd=1.0, extra=None):
        import json
        yield ExecEvent(kind=EventKind.phase, phase="running", message="[planner] planning")
        reg = get_tool_registry()
        r1 = await reg.call("propose_subtasks", json.dumps({"items": self._items}),
                            workdir=workdir, context=extra)
        r2 = await reg.call("set_next_action",
                            json.dumps({"next_action": self._next_action}),
                            workdir=workdir, context=extra)
        usage = Usage(tokens_in=10, tokens_out=5, cost_usd=0.01)
        result = ExecResult(text=f"Planned.\n{r1}\n{r2}", usage=usage,
                            provider=self.name, model="planner-v1")
        yield ExecEvent(kind=EventKind.result, message="[planner] done",
                        data={"result": result, "usage": usage.__dict__})


@pytest.fixture
async def planner_env(tmp_path, monkeypatch):
    """Fresh DB + planner/tools DB handles + a fake planning executor."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/p.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="T"))
        db.add(Project(id="pr", owner_id="local", name="P", description="a project"))
        db.add(WorkItem(id=1, owner_id="local", project_id="pr", title="WI",
                        objective="ship the thing"))
        await db.commit()

    import app.planner as planner
    import app.providers.tools.planner_tools as pt

    monkeypatch.setattr(planner, "AsyncSessionLocal", Maker)
    monkeypatch.setattr(pt, "AsyncSessionLocal", Maker)
    monkeypatch.setattr(planner, "get_executor", lambda name: _PlanningExecutor(name=name))
    yield Maker, planner


# ── selection resolution ─────────────────────────────────────────────────────

class TestSelection:
    def test_explicit_request_wins(self):
        from app.planner import resolve_planner_selection
        proj = Project(id="p", owner_id="local", name="P", sensitivity="normal")
        prov, model, local = resolve_planner_selection(
            proj, requested_provider="claude", requested_model="claude-x"
        )
        assert prov == "claude" and model == "claude-x" and local is False

    def test_falls_back_to_project_default(self):
        from app.planner import resolve_planner_selection
        proj = Project(id="p", owner_id="local", name="P", sensitivity="normal",
                       planner_provider="grok", planner_model="grok-m")
        prov, model, local = resolve_planner_selection(proj)
        assert prov == "grok" and model == "grok-m"

    def test_confidential_pins_local(self, monkeypatch):
        import app.planner as planner
        monkeypatch.setattr(planner, "is_local_instance", lambda p: p == "ollama")
        monkeypatch.setattr(planner, "local_candidate_ids", lambda: ["ollama"])
        proj = Project(id="p", owner_id="local", name="P", sensitivity="confidential")
        prov, model, local = planner.resolve_planner_selection(
            proj, requested_provider="claude", requested_model="claude-x"
        )
        assert prov == "ollama" and model is None and local is True

    def test_confidential_no_local_raises(self, monkeypatch):
        import app.planner as planner
        monkeypatch.setattr(planner, "is_local_instance", lambda p: False)
        monkeypatch.setattr(planner, "local_candidate_ids", lambda: [])
        proj = Project(id="p", owner_id="local", name="P", sensitivity="confidential")
        with pytest.raises(planner.PlannerError):
            planner.resolve_planner_selection(proj, requested_provider="claude")

    def test_no_provider_available_raises(self, monkeypatch):
        import app.planner as planner
        monkeypatch.setattr(planner, "list_instances", lambda: [])
        proj = Project(id="p", owner_id="local", name="P", sensitivity="normal")
        with pytest.raises(planner.PlannerError):
            planner.resolve_planner_selection(proj)


# ── planning-mode gating in the executor ─────────────────────────────────────

class TestPlanningMode:
    def test_planning_offers_only_planner_tools(self):
        from app.providers.model_executor import _active_tool_schemas
        names = {s["name"] for s in _active_tool_schemas({"planning": True})}
        assert names == {"propose_subtasks", "set_next_action"}

    def test_base_run_excludes_planner_tools(self):
        from app.providers.model_executor import _active_tool_schemas
        names = {s["name"] for s in _active_tool_schemas({})}
        assert "propose_subtasks" not in names and "set_next_action" not in names

    def test_planning_prompt_is_planner_flavored(self):
        from app.providers.model_executor import _base_system_prompt
        assert "planner" in _base_system_prompt({"planning": True}).lower()


# ── tools + end-to-end drive ─────────────────────────────────────────────────

class TestPlanningTurn:
    @pytest.mark.asyncio
    async def test_run_proposes_subtasks_and_sets_next_action(self, planner_env):
        Maker, planner = planner_env
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert run.status == "succeeded"
            assert run.proposals["subtasks_proposed"] == 2
            assert run.proposals["next_action"] == "ship it"
            wi = await db.get(WorkItem, 1)
            proposed = [i for i in wi.subtasks["items"] if i["status"] == "proposed"]
            assert {p["label"] for p in proposed} == {"the report", "review notes"}
            # proposer-only: proposed, not confirmed/done
            assert all(not p["verified"] and not p["done"] for p in proposed)
            assert proposed[0]["proposed_by"] == "planner"
            assert wi.next_action == "ship it"

    @pytest.mark.asyncio
    async def test_propose_subtasks_tool_requires_bound_work_item(self, planner_env):
        from app.providers.tools import planner_tools
        out = await planner_tools.propose_subtasks(
            [{"label": "x"}], context={"owner_id": "local"}  # no work_item_id
        )
        assert "error" in out.lower()

    @pytest.mark.asyncio
    async def test_confidential_project_run_pins_local(self, planner_env, monkeypatch):
        Maker, planner = planner_env
        monkeypatch.setattr(planner, "is_local_instance", lambda p: p == "ollama")
        monkeypatch.setattr(planner, "local_candidate_ids", lambda: ["ollama"])
        monkeypatch.setattr(planner, "get_executor",
                            lambda name: _PlanningExecutor(name=name, items=[{"label": "a"}]))
        async with Maker() as db:
            proj = await db.get(Project, "pr")
            proj.sensitivity = "confidential"
            await db.commit()
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, owner_id="local", provider="claude"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert run.provider == "ollama" and run.local_pinned is True


# ── HTTP lane ────────────────────────────────────────────────────────────────

class TestPlannerApi:
    def _client(self, tmp_path, monkeypatch):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add(Owner(id="local", label="T"))
                db.add(Project(id="pr", owner_id="local", name="P"))
                db.add(WorkItem(id=1, owner_id="local", project_id="pr", title="WI"))
                await db.commit()
            return Maker

        Maker = asyncio.get_event_loop().run_until_complete(_setup())

        import app.planner as planner
        import app.providers.tools.planner_tools as pt
        monkeypatch.setattr(planner, "AsyncSessionLocal", Maker)
        monkeypatch.setattr(pt, "AsyncSessionLocal", Maker)
        monkeypatch.setattr(planner, "get_executor", lambda name: _PlanningExecutor(name=name))
        # Don't fire the real background drive during the request test; assert the
        # row is created running, then drive it explicitly.
        monkeypatch.setattr(planner, "schedule_drive", lambda run_id: None)

        async def _override():
            async with Maker() as s:
                yield s

        app.dependency_overrides[get_db] = _override
        app.dependency_overrides[_owner_id] = lambda: "local"
        return TestClient(app), Maker, planner

    def test_plan_endpoint_creates_running_run_then_drives(self, tmp_path, monkeypatch):
        client, Maker, planner = self._client(tmp_path, monkeypatch)
        try:
            r = client.post("/api/work-items/1/plan",
                            json={"message": "plan it", "provider": "planner-mock"})
            assert r.status_code == 202, r.text
            body = r.json()
            assert body["status"] == "running" and body["work_item_id"] == 1
            run_id = body["id"]

            # Drive it (the endpoint deferred the background task in this test).
            asyncio.get_event_loop().run_until_complete(planner.drive_planning_turn(run_id))

            poll = client.get(f"/api/planner-runs/{run_id}")
            assert poll.status_code == 200
            assert poll.json()["status"] == "succeeded"
            assert poll.json()["proposals"]["subtasks_proposed"] == 2

            hist = client.get("/api/work-items/1/planner-runs")
            assert hist.status_code == 200 and len(hist.json()) == 1
        finally:
            app.dependency_overrides.clear()

    def test_plan_unknown_work_item_404(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            r = client.post("/api/work-items/999/plan", json={})
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()
