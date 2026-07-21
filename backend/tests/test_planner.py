"""
tests/test_planner.py — the per-project planner agent lane (P-0078, slices 1–2).

Covers the seams the planner reuses without becoming a new engine:
  - selection resolution (fallback + the confidential→local sovereignty fence);
  - the planning-mode toolset/prompt gating in the model executor, including the
    slice-2 split by scope (item tools vs project tools);
  - the proposer-only tools landing on a WorkItem/Project via the registry, driven
    end-to-end through run_planning_turn — including the structural ones, which mint
    work items in the `proposed` state rather than real work; and
  - the HTTP lane (POST /plan → 202 running, poll GET), both scopes.
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


class _ProjectPlanningExecutor(Executor):
    """A fake project-level planner: records a digest and triages one work item,
    exercising the project half of the toolset through the registry."""

    name = "project-planner-mock"
    tier = "mock"

    def __init__(self, name: str = "project-planner-mock", *, summary=None, triage=None) -> None:
        self.name = name
        self.tier = "mock"
        self._summary = summary if summary is not None else {
            "headline": "two items open, one stalled", "focus": [1], "stalled": [],
        }
        self._triage = triage if triage is not None else {
            "title": "write the runbook", "objective": "a cold operator can restore",
        }

    @property
    def kind(self) -> str:
        return "mock"

    def is_healthy(self) -> bool:
        return True

    async def run_stream(self, prompt, *, workdir, tools_enabled=True,
                         max_rounds=10, budget_usd=1.0, extra=None):
        import json
        reg = get_tool_registry()
        r1 = await reg.call("summarize_project", json.dumps(self._summary),
                            workdir=workdir, context=extra)
        r2 = await reg.call("triage_signal", json.dumps(self._triage),
                            workdir=workdir, context=extra)
        usage = Usage(tokens_in=8, tokens_out=4, cost_usd=0.005)
        result = ExecResult(text=f"Read the ledger.\n{r1}\n{r2}", usage=usage,
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
    def test_item_turn_offers_only_item_tools(self):
        from app.providers.model_executor import _active_tool_schemas
        names = {s["name"] for s in _active_tool_schemas({"planning": True, "work_item_id": 1})}
        assert names == {"propose_subtasks", "set_next_action", "decompose"}

    def test_project_turn_offers_only_project_tools(self):
        """A project-level turn has no bound work item, so the item tools could only
        answer "nothing is bound" — it is handed the project half instead."""
        from app.providers.model_executor import _active_tool_schemas
        names = {s["name"] for s in _active_tool_schemas({"planning": True})}
        assert names == {"triage_signal", "summarize_project"}

    def test_base_run_excludes_every_planner_tool(self):
        from app.providers.model_executor import _active_tool_schemas
        from app.providers.tools.registry import PLANNER_TOOL_NAMES
        names = {s["name"] for s in _active_tool_schemas({})}
        assert names.isdisjoint(PLANNER_TOOL_NAMES)

    def test_planning_turn_excludes_exec_tools(self):
        """The fence cuts both ways: a planning turn cannot reach code_exec even when
        the run would otherwise be allowed it."""
        from app.providers.model_executor import _active_tool_schemas
        names = {s["name"] for s in _active_tool_schemas(
            {"planning": True, "work_item_id": 1, "exec_policy": "always", "human_in_loop": True}
        )}
        assert "code_exec" not in names

    def test_planning_prompt_is_planner_flavored(self):
        from app.providers.model_executor import _base_system_prompt
        assert "planner" in _base_system_prompt({"planning": True, "work_item_id": 1}).lower()

    def test_project_planning_prompt_differs_from_item_prompt(self):
        from app.providers.model_executor import _base_system_prompt
        item = _base_system_prompt({"planning": True, "work_item_id": 1})
        project = _base_system_prompt({"planning": True})
        assert item != project
        assert "summarize_project" in project and "triage_signal" in project


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


# ── structural tools: decompose / triage (slice 2) ───────────────────────────

class TestStructuralTools:
    """The structural tools mint whole work items — durable intent. Proposer-only
    means they land `proposed`, doing nothing until the operator accepts them."""

    @pytest.mark.asyncio
    async def test_decompose_creates_proposed_children(self, planner_env):
        from sqlalchemy import select

        from app.providers.tools import planner_tools
        Maker, _ = planner_env
        out = await planner_tools.decompose(
            [{"title": "survey the options", "objective": "pick one", "risk": "medium"},
             {"title": "write the migration"}],
            context={"owner_id": "local", "project_id": "pr", "work_item_id": 1},
        )
        assert "proposed 2 child" in out
        async with Maker() as db:
            kids = list((await db.execute(
                select(WorkItem).where(WorkItem.parent_id == 1).order_by(WorkItem.id)
            )).scalars().all())
        assert len(kids) == 2
        # Proposals, not work: nothing is actionable until an operator accepts.
        assert all(k.state == "proposed" for k in kids)
        assert all(k.project_id == "pr" for k in kids)
        assert kids[0].risk == "medium" and kids[1].risk == "low"
        assert kids[0].signal["kind"] == "decompose"

    @pytest.mark.asyncio
    async def test_decompose_requires_bound_work_item(self, planner_env):
        from app.providers.tools import planner_tools
        out = await planner_tools.decompose(
            [{"title": "x"}], context={"owner_id": "local", "project_id": "pr"}
        )
        assert "error" in out.lower()

    @pytest.mark.asyncio
    async def test_decompose_caps_runaway_output(self, planner_env):
        from app.providers.tools import planner_tools
        out = await planner_tools.decompose(
            [{"title": f"child {i}"} for i in range(25)],
            context={"owner_id": "local", "project_id": "pr", "work_item_id": 1},
        )
        assert "at most" in out

    @pytest.mark.asyncio
    async def test_triage_creates_proposed_top_level_item(self, planner_env):
        from app.providers.tools import planner_tools
        Maker, _ = planner_env
        out = await planner_tools.triage_signal(
            "restore is untested", objective="a restore runs green", risk="high",
            source="gap in the ledger",
            context={"owner_id": "local", "project_id": "pr"},
        )
        assert "proposed work item" in out
        item_id = int(out.split("#")[1].split()[0])
        async with Maker() as db:
            item = await db.get(WorkItem, item_id)
        assert item.state == "proposed" and item.parent_id is None
        assert item.risk == "high" and item.signal["kind"] == "triage"
        assert item.signal["source"] == "gap in the ledger"

    @pytest.mark.asyncio
    async def test_triage_requires_project(self, planner_env):
        from app.providers.tools import planner_tools
        out = await planner_tools.triage_signal("x", context={"owner_id": "local"})
        assert "error" in out.lower()

    @pytest.mark.asyncio
    async def test_triage_rejects_another_owners_project(self, planner_env):
        """Dispatched by name off model output, so it re-checks ownership itself
        rather than trusting the lane that happens to call it today."""
        from app.providers.tools import planner_tools
        out = await planner_tools.triage_signal(
            "x", context={"owner_id": "someone-else", "project_id": "pr"}
        )
        assert "project not found" in out


# ── summarize_project (slice 2) ──────────────────────────────────────────────

class TestSummarize:
    async def _run(self, Maker) -> int:
        async with Maker() as db:
            run = PlannerRun(owner_id="local", project_id="pr", status="running")
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run.id

    @pytest.mark.asyncio
    async def test_digest_lands_on_the_run(self, planner_env):
        from app.providers.tools import planner_tools
        Maker, _ = planner_env
        run_id = await self._run(Maker)
        out = await planner_tools.summarize_project(
            "one item open", focus=[1], notes="nothing is blocked",
            context={"owner_id": "local", "project_id": "pr", "planner_run_id": run_id},
        )
        assert "digest recorded" in out
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert run.proposals["summary"]["headline"] == "one item open"
        assert run.proposals["summary"]["focus"] == [1]
        assert run.proposals["summary"]["notes"] == "nothing is blocked"

    @pytest.mark.asyncio
    async def test_unknown_work_item_ids_are_dropped(self, planner_env):
        """The digest is grounded: it cannot point at work items that do not exist,
        so a hallucinated id never reaches the operator as a real reference."""
        from app.providers.tools import planner_tools
        Maker, _ = planner_env
        run_id = await self._run(Maker)
        out = await planner_tools.summarize_project(
            "status", focus=[1, 4242], stalled=["nonsense"],
            context={"owner_id": "local", "project_id": "pr", "planner_run_id": run_id},
        )
        assert "ignored" in out
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert run.proposals["summary"]["focus"] == [1]
        assert run.proposals["summary"]["stalled"] == []

    @pytest.mark.asyncio
    async def test_summarize_mutates_no_work_item(self, planner_env):
        from app.providers.tools import planner_tools
        Maker, _ = planner_env
        run_id = await self._run(Maker)
        async with Maker() as db:
            before = (await db.get(WorkItem, 1)).state
        await planner_tools.summarize_project(
            "status",
            context={"owner_id": "local", "project_id": "pr", "planner_run_id": run_id},
        )
        async with Maker() as db:
            wi = await db.get(WorkItem, 1)
        assert wi.state == before and wi.subtasks is None


# ── project-level planning turn (slice 2) ────────────────────────────────────

class TestProjectPlanningTurn:
    @pytest.mark.asyncio
    async def test_project_turn_summarizes_and_triages(self, planner_env, monkeypatch):
        from sqlalchemy import select
        Maker, planner = planner_env
        monkeypatch.setattr(planner, "get_executor",
                            lambda name: _ProjectPlanningExecutor(name=name))
        run_id = await planner.run_planning_turn(
            "pr", owner_id="local", provider="project-planner-mock"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            triaged = list((await db.execute(
                select(WorkItem).where(WorkItem.state == "proposed")
            )).scalars().all())
        assert run.status == "succeeded" and run.work_item_id is None
        assert run.proposals["summary"]["headline"] == "two items open, one stalled"
        # The run's own audit row attributes exactly what it minted.
        assert run.proposals["work_items_proposed"] == [t.id for t in triaged]
        assert len(triaged) == 1 and triaged[0].title == "write the runbook"

    @pytest.mark.asyncio
    async def test_project_prompt_carries_the_open_ledger(self, planner_env, monkeypatch):
        """The ledger is the project planner's whole input — closed work is left out
        so it reasons about what to do next, not about finished history."""
        Maker, planner = planner_env
        monkeypatch.setattr(planner, "get_executor",
                            lambda name: _ProjectPlanningExecutor(name=name))
        async with Maker() as db:
            db.add(WorkItem(id=2, owner_id="local", project_id="pr", title="shipped thing",
                            state="done"))
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="project-planner-mock"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert "#1 [open] WI" in run.request
        assert "shipped thing" not in run.request

    @pytest.mark.asyncio
    async def test_item_turn_rollup_merges_with_tool_written_proposals(self, planner_env):
        """_finish must merge into `proposals`, not replace it — the tools have
        already written their attributed entries there during the drive."""
        Maker, planner = planner_env
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            run.proposals = {**run.proposals, "work_items_proposed": [99]}
            await db.commit()
        # Re-finish (as a second drive would) and confirm nothing is clobbered.
        await planner._finish(run_id, status="succeeded", work_item_id=1)
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert run.proposals["work_items_proposed"] == [99]
        assert run.proposals["subtasks_proposed"] == 2


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

    def test_project_plan_endpoint_triages_into_proposed_items(self, tmp_path, monkeypatch):
        client, Maker, planner = self._client(tmp_path, monkeypatch)
        try:
            monkeypatch.setattr(planner, "get_executor",
                                lambda name: _ProjectPlanningExecutor(name=name))
            r = client.post("/api/projects/pr/plan", json={"provider": "project-planner-mock"})
            assert r.status_code == 202, r.text
            body = r.json()
            assert body["status"] == "running" and body["work_item_id"] is None
            run_id = body["id"]

            asyncio.get_event_loop().run_until_complete(planner.drive_planning_turn(run_id))

            poll = client.get(f"/api/planner-runs/{run_id}").json()
            assert poll["status"] == "succeeded"
            assert poll["proposals"]["summary"]["headline"]

            # The triaged item is visible to the operator as a proposal, and the
            # accept edge is the one the state machine offers.
            proposed = client.get("/api/projects/pr/work-items?state=proposed").json()
            assert len(proposed) == 1
            accepted = client.patch(f"/api/work-items/{proposed[0]['id']}",
                                    json={"state": "open"})
            assert accepted.status_code == 200 and accepted.json()["state"] == "open"

            hist = client.get("/api/projects/pr/planner-runs").json()
            assert len(hist) == 1 and hist[0]["id"] == run_id
        finally:
            app.dependency_overrides.clear()

    def test_plan_unknown_project_404(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            assert client.post("/api/projects/nope/plan", json={}).status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_plan_unknown_work_item_404(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            r = client.post("/api/work-items/999/plan", json={})
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()
