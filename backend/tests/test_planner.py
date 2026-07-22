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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_db
from app.main import _owner_id, app
from app.models import ContextSource, Owner, PlannerRun, Project, WorkItem
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
    async def test_prompt_carries_the_declared_context_inventory(self, planner_env):
        """A project whose substance lives in its declared context (a wiki, a spec
        set) read as empty to the planner, which then correctly reported having
        nothing to say. Paths only — the prompt never carries source content."""
        from app.models import ContextSource
        Maker, planner = planner_env
        async with Maker() as db:
            db.add(ContextSource(owner_id="local", project_id="pr", kind="dir",
                                 rel_path="context/world", domain="lore",
                                 bootstrap_order=1, last_revision="abc123def456789"))
            db.add(ContextSource(owner_id="local", project_id="pr", kind="file",
                                 rel_path="context/rules.md"))
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "Declared context sources (2)" in req
        assert "context/world [dir] · lore · rev abc123def456" in req
        assert "context/rules.md [file] · unhashed" in req

    @pytest.mark.asyncio
    async def test_empty_project_is_told_to_bootstrap_not_to_report_emptiness(
        self, planner_env
    ):
        """The reported defect: a project with no open work items produced
        "no data · proposed nothing". With nothing to react to, bootstrapping is
        the job — the prompt has to say so."""
        Maker, planner = planner_env
        async with Maker() as db:
            wi = await db.get(WorkItem, 1)
            wi.state = "done"
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "(none open)" in req
        assert "bootstrapping is exactly the job" in req
        assert "Do not answer that there is nothing to report" in req

    @pytest.mark.asyncio
    async def test_closed_work_is_counted_not_silently_dropped(self, planner_env):
        """A finished project is not an empty one; the planner cannot tell them
        apart from an empty open-ledger alone."""
        Maker, planner = planner_env
        async with Maker() as db:
            db.add(WorkItem(id=2, owner_id="local", project_id="pr", title="shipped",
                            state="done"))
            db.add(WorkItem(id=3, owner_id="local", project_id="pr", title="abandoned",
                            state="dropped"))
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "Also 2 closed work item(s)" in req
        # Still summarized, not listed — closed detail is not the planner's input.
        assert "shipped" not in req and "abandoned" not in req

    @pytest.mark.asyncio
    async def test_excerpts_ground_the_planner_in_what_the_project_says(
        self, planner_env, tmp_path
    ):
        """The point of excerpting: the planner reasons from the project's content,
        not from a list of filenames."""
        from app.models import ContextSource
        Maker, planner = planner_env
        root = tmp_path / "wiki"
        (root / "context").mkdir(parents=True)
        (root / "context" / "rules.md").write_text(
            "Magic is bounded by the ley network.", encoding="utf-8"
        )
        async with Maker() as db:
            proj = await db.get(Project, "pr")
            proj.root_path = str(root)
            db.add(ContextSource(owner_id="local", project_id="pr", kind="file",
                                 rel_path="context/rules.md"))
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "## Context excerpts" in req
        assert "Magic is bounded by the ley network." in req
        # Excerpts are reference material, not commands to the planner.
        assert "not instructions" in req

    @pytest.mark.asyncio
    async def test_confidential_source_is_not_excerpted_to_a_remote_planner(
        self, planner_env, tmp_path, monkeypatch
    ):
        """The P-0009 fence at source granularity. A confidential *project* is
        already local-pinned; this is the case that misses — a normal project
        holding one confidential source."""
        from app.models import ContextSource
        Maker, planner = planner_env
        root = tmp_path / "p"
        (root / "secret").mkdir(parents=True)
        (root / "secret" / "keys.md").write_text("SEKRIT ROSTER", encoding="utf-8")
        (root / "open.md").write_text("public notes", encoding="utf-8")
        async with Maker() as db:
            proj = await db.get(Project, "pr")
            proj.root_path = str(root)
            proj.sensitivity = "normal"
            db.add(ContextSource(owner_id="local", project_id="pr", kind="dir",
                                 rel_path="secret", sensitivity="confidential"))
            db.add(ContextSource(owner_id="local", project_id="pr", kind="file",
                                 rel_path="open.md", sensitivity="inherit"))
            await db.commit()

        monkeypatch.setattr(planner, "is_local_instance", lambda p: False)
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="remote-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "SEKRIT ROSTER" not in req
        assert "public notes" in req
        # Withheld, and *said* to be withheld — a partial view must announce itself.
        assert "Context NOT shown to you" in req
        assert "secret — confidential source, remote planner" in req

    @pytest.mark.asyncio
    async def test_confidential_source_is_excerpted_to_a_local_planner(
        self, planner_env, tmp_path, monkeypatch
    ):
        from app.models import ContextSource
        Maker, planner = planner_env
        root = tmp_path / "p2"
        root.mkdir(parents=True)
        (root / "notes.md").write_text("SEKRIT ROSTER", encoding="utf-8")
        async with Maker() as db:
            proj = await db.get(Project, "pr")
            proj.root_path = str(root)
            db.add(ContextSource(owner_id="local", project_id="pr", kind="file",
                                 rel_path="notes.md", sensitivity="confidential"))
            await db.commit()

        monkeypatch.setattr(planner, "is_local_instance", lambda p: True)
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="ollama"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "SEKRIT ROSTER" in req

    @pytest.mark.asyncio
    async def test_excerpting_cannot_escape_the_project_root(self, planner_env, tmp_path):
        """Scope fence: a planning turn reads this project's context and nothing
        else. Cross-project context is operator-approved, not a planner default."""
        Maker, planner = planner_env
        other = tmp_path / "other-project"
        other.mkdir()
        (other / "theirs.md").write_text("ANOTHER PROJECT", encoding="utf-8")
        root = tmp_path / "mine"
        root.mkdir()
        async with Maker() as db:
            proj = await db.get(Project, "pr")
            proj.root_path = str(root)
            db.add(ContextSource(owner_id="local", project_id="pr", kind="file",
                                 rel_path="../other-project/theirs.md"))
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        assert "ANOTHER PROJECT" not in req
        assert "unsafe path" in req

    @pytest.mark.asyncio
    async def test_excerpts_respect_the_byte_budget(self, planner_env, tmp_path):
        Maker, planner = planner_env
        root = tmp_path / "big"
        root.mkdir()
        (root / "huge.md").write_text("x" * 100_000, encoding="utf-8")
        async with Maker() as db:
            proj = await db.get(Project, "pr")
            proj.root_path = str(root)
            db.add(ContextSource(owner_id="local", project_id="pr", kind="file",
                                 rel_path="huge.md"))
            await db.commit()
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            req = (await db.get(PlannerRun, run_id)).request
        budget = planner._settings.planner_excerpt_budget_bytes
        assert req.count("x") <= budget
        assert "truncated" in req  # the cut is stated, not silent

    @pytest.mark.asyncio
    async def test_prompt_is_surfaced_on_the_run(self, planner_env):
        """"It proposed nothing" is un-diagnosable without seeing what it was told."""
        from app.schemas import PlannerRunOut
        Maker, planner = planner_env
        run_id = await planner.start_planning_turn(
            "pr", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            out = PlannerRunOut.model_validate(await db.get(PlannerRun, run_id))
        assert out.request and "# Project: P" in out.request

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


# ── the lane always terminates ───────────────────────────────────────────────

class TestNeverStrandsARun:
    """A `running` PlannerRun is unrecoverable — nothing polls it, nothing
    reconciles it, and the operator watches a spinner forever. Every exit from the
    drive must therefore leave the row terminal. These are the paths that stranded a
    real turn in Docker: a provider that hangs, a crash outside the stream loop,
    cancellation at shutdown, and a process that dies mid-drive."""

    @pytest.mark.asyncio
    async def test_hung_provider_times_out(self, planner_env, monkeypatch):
        Maker, planner = planner_env

        class _Hanging(_PlanningExecutor):
            async def run_stream(self, prompt, *, workdir, tools_enabled=True,
                                 max_rounds=10, budget_usd=1.0, extra=None):
                await asyncio.sleep(3600)
                yield  # pragma: no cover — never reached

        monkeypatch.setattr(planner, "get_executor", lambda name: _Hanging(name=name))
        monkeypatch.setattr(planner._settings, "planner_timeout_seconds", 1)
        run_id = await planner.start_planning_turn(
            "pr", work_item_id=1, owner_id="local", provider="planner-mock"
        )
        await planner.drive_planning_turn(run_id)
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert run.status == "failed" and "exceeded" in run.error
        assert run.finished_at is not None

    @pytest.mark.asyncio
    async def test_crash_outside_the_stream_still_finishes(self, planner_env, monkeypatch):
        """get_executor raising (not returning None) used to escape the drive
        entirely, since the try block only wrapped the stream loop."""
        Maker, planner = planner_env

        def _boom(name):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(planner, "get_executor", _boom)
        run_id = await planner.start_planning_turn(
            "pr", work_item_id=1, owner_id="local", provider="planner-mock"
        )
        await planner.drive_planning_turn(run_id)
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert run.status == "failed" and "registry exploded" in run.error

    @pytest.mark.asyncio
    async def test_cancellation_marks_the_row_before_propagating(self, planner_env, monkeypatch):
        """CancelledError is a BaseException, so a bare `except Exception` misses it
        and the row is stranded at shutdown."""
        Maker, planner = planner_env

        class _Cancelled(_PlanningExecutor):
            async def run_stream(self, prompt, *, workdir, tools_enabled=True,
                                 max_rounds=10, budget_usd=1.0, extra=None):
                raise asyncio.CancelledError()
                yield  # pragma: no cover

        monkeypatch.setattr(planner, "get_executor", lambda name: _Cancelled(name=name))
        run_id = await planner.start_planning_turn(
            "pr", work_item_id=1, owner_id="local", provider="planner-mock"
        )
        with pytest.raises(asyncio.CancelledError):
            await planner.drive_planning_turn(run_id)
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
        assert run.status == "failed" and "cancelled" in run.error

    @pytest.mark.asyncio
    async def test_startup_reaper_clears_rows_orphaned_by_a_restart(self, planner_env):
        Maker, planner = planner_env
        async with Maker() as db:
            db.add(PlannerRun(owner_id="local", project_id="pr", status="running"))
            db.add(PlannerRun(owner_id="local", project_id="pr", status="succeeded"))
            await db.commit()

        assert await planner.reap_orphaned_planner_runs() == 1
        async with Maker() as db:
            rows = list((await db.execute(
                select(PlannerRun).order_by(PlannerRun.id)
            )).scalars().all())
        assert rows[0].status == "failed" and "restart" in rows[0].error
        assert rows[0].finished_at is not None
        assert rows[1].status == "succeeded"  # terminal rows are left alone
        # Idempotent: a second boot has nothing left to reap.
        assert await planner.reap_orphaned_planner_runs() == 0

    @pytest.mark.asyncio
    async def test_planning_does_not_inherit_the_run_timeout(self):
        """Planning is cheap, frequent meta-work; inheriting the 30-minute run budget
        is what let a turn outlive the operator's patience by an order of magnitude."""
        from app.config import get_settings
        s = get_settings()
        assert s.planner_timeout_seconds < s.run_timeout_seconds


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

    def test_planner_settings_round_trip(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            from app.providers import registry as preg
            monkeypatch.setattr(
                preg, "get_instance",
                lambda i: object() if i in ("claude", "ollama") else None,
            )

            got = client.get("/api/projects/pr/planner").json()
            assert got["provider"] is None  # never a mandatory per-project decision

            saved = client.put("/api/projects/pr/planner",
                               json={"provider": "claude", "model": "claude-x"})
            assert saved.status_code == 200, saved.text
            assert saved.json()["provider"] == "claude"
            assert saved.json()["effective_provider"] == "claude"
            assert saved.json()["note"] is None  # stored == what runs
            assert client.get("/api/projects/pr").json()["planner_provider"] == "claude"

            cleared = client.put("/api/projects/pr/planner", json={})
            assert cleared.json()["provider"] is None
        finally:
            app.dependency_overrides.clear()

    def test_unknown_provider_refused_at_set_time(self, tmp_path, monkeypatch):
        """A typo that only surfaces when a planning turn fails is a much worse
        trade than a 400 at the moment the operator sets it."""
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            from app.providers import registry as preg
            monkeypatch.setattr(preg, "get_instance", lambda i: None)
            r = client.put("/api/projects/pr/planner", json={"provider": "cluade"})
            assert r.status_code == 400 and "unknown provider" in r.json()["detail"]
        finally:
            app.dependency_overrides.clear()

    def test_model_without_provider_refused(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            r = client.put("/api/projects/pr/planner", json={"model": "some-model"})
            assert r.status_code == 400
        finally:
            app.dependency_overrides.clear()

    def test_settings_surface_the_sovereignty_fence(self, tmp_path, monkeypatch):
        """A confidential project shows the operator what would *actually* run, rather
        than leaving the UI to re-derive the fence and risk disagreeing with the lane."""
        client, Maker, _ = self._client(tmp_path, monkeypatch)
        try:
            import app.planner as planner
            from app.providers import registry as preg
            monkeypatch.setattr(preg, "get_instance", lambda i: object())
            monkeypatch.setattr(planner, "is_local_instance", lambda p: p == "ollama")
            monkeypatch.setattr(planner, "local_candidate_ids", lambda: ["ollama"])

            async def _confidential():
                async with Maker() as db:
                    proj = await db.get(Project, "pr")
                    proj.sensitivity = "confidential"
                    proj.planner_provider = "claude"
                    await db.commit()

            asyncio.get_event_loop().run_until_complete(_confidential())

            got = client.get("/api/projects/pr/planner").json()
            assert got["provider"] == "claude"           # what the operator chose
            assert got["effective_provider"] == "ollama"  # what the fence allows
            assert got["local_pinned"] is True
            assert "confidential" in got["note"]
        finally:
            app.dependency_overrides.clear()

    def test_settings_report_when_nothing_can_run(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            import app.planner as planner
            monkeypatch.setattr(planner, "list_instances", lambda: [])
            got = client.get("/api/projects/pr/planner").json()
            assert got["effective_provider"] is None and got["note"]
        finally:
            app.dependency_overrides.clear()

    def test_planner_run_history_is_bounded(self, tmp_path, monkeypatch):
        client, Maker, _ = self._client(tmp_path, monkeypatch)
        try:
            async def _seed():
                async with Maker() as db:
                    for _ in range(5):
                        db.add(PlannerRun(owner_id="local", project_id="pr",
                                          status="succeeded"))
                    await db.commit()

            asyncio.get_event_loop().run_until_complete(_seed())
            rows = client.get("/api/projects/pr/planner-runs?limit=2").json()
            assert len(rows) == 2
            assert rows[0]["id"] > rows[1]["id"]  # newest first
        finally:
            app.dependency_overrides.clear()

    def test_plan_unknown_work_item_404(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        try:
            r = client.post("/api/work-items/999/plan", json={})
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


# ── plan/CLI lane parity (P-0082) ────────────────────────────────────────────

class _TextOnlyPlanningExecutor(Executor):
    """A CLI-shaped planner: returns text and nothing else.

    This is the whole defect in one class. `CLIExecutor` shells out to a binary
    and never consults the tool registry, so a planning turn on a `cli` provider
    was offered no tools — six runs on the live instance produced prose and zero
    structural output while five recorded `succeeded`. This fake calls no tools
    for exactly that reason; if the lane works, it works through the reply text.
    """

    name = "cli-planner-mock"
    tier = "mock"

    def __init__(self, name: str = "cli-planner-mock", *, text: str = "") -> None:
        self.name = name
        self.tier = "mock"
        self._text = text

    @property
    def kind(self) -> str:
        return "cli"

    def is_healthy(self) -> bool:
        return True

    async def run_stream(self, prompt, *, workdir, tools_enabled=True,
                         max_rounds=10, budget_usd=1.0, extra=None):
        usage = Usage(tokens_in=7, tokens_out=3, cost_usd=0.0)
        result = ExecResult(text=self._text, usage=usage,
                            provider=self.name, model="cli-planner")
        yield ExecEvent(kind=EventKind.result, message="[planner] done",
                        data={"result": result, "usage": usage.__dict__})


class TestPlanLaneParity:
    """The plan/CLI lane reaches the same tools through a text protocol."""

    def _cli_env(self, planner_env, monkeypatch, text: str):
        Maker, planner = planner_env
        monkeypatch.setattr(planner, "_uses_protocol", lambda provider_id: True)
        monkeypatch.setattr(
            planner, "get_executor",
            lambda name: _TextOnlyPlanningExecutor(name=name, text=text),
        )
        return Maker, planner

    @pytest.mark.asyncio
    async def test_item_scope_block_lands_real_subtasks(self, planner_env, monkeypatch):
        block = (
            "Here is how I would break this down.\n\n"
            "```batonkeep-plan\n"
            '[{"tool": "propose_subtasks", "args": {"items": ['
            '{"label": "draft the spec", "expected": "spec.md"},'
            '{"label": "review it"}]}},'
            '{"tool": "set_next_action", "args": {"next_action": "start the spec"}}]\n'
            "```\n"
        )
        Maker, planner = self._cli_env(planner_env, monkeypatch, block)
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="grok"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert run.status == "succeeded"
            # The parity claim: the same structural record the API lane produces.
            assert run.proposals["subtasks_proposed"] == 2
            assert run.proposals["next_action"] == "start the spec"
            wi = await db.get(WorkItem, 1)
            proposed = [i for i in wi.subtasks["items"] if i["status"] == "proposed"]
            assert {p["label"] for p in proposed} == {"draft the spec", "review it"}
            # Still proposer-only — the transport changes nothing about authority.
            assert all(p["proposed_by"] == "planner" for p in proposed)
            assert all(not p["verified"] and not p["done"] for p in proposed)
            # Prose kept for the operator; the block is not left in it.
            assert "how I would break this down" in run.response
            assert "batonkeep-plan" not in run.response

    @pytest.mark.asyncio
    async def test_prose_without_a_block_proposes_nothing(self, planner_env, monkeypatch):
        """R3 run #5 exactly: a credible plan in prose, nothing recorded. It must
        stay visible and must not invent structure that was never committed to."""
        Maker, planner = self._cli_env(
            planner_env, monkeypatch,
            "1. Fix the planner. 2. Rerun the regression. 3. Audit after.",
        )
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="grok"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert run.proposals.get("subtasks_proposed") in (0, None)
            assert "Fix the planner" in run.response
            wi = await db.get(WorkItem, 1)
            assert not (wi.subtasks or {}).get("items")

    @pytest.mark.asyncio
    async def test_out_of_scope_call_is_refused_not_obeyed(self, planner_env, monkeypatch):
        """Scope is enforced at dispatch, not trusted from the block: an item turn
        may not reach the project tools even if the model asks."""
        block = ('```batonkeep-plan\n'
                 '[{"tool": "summarize_project", "args": {"headline": "sneaky"}}]\n```')
        Maker, planner = self._cli_env(planner_env, monkeypatch, block)
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="grok"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert "not available on this planning scope" in (run.error or "")
            assert "summary" not in (run.proposals or {})

    @pytest.mark.asyncio
    async def test_repeated_identical_call_is_applied_once(self, planner_env, monkeypatch):
        """Models restate their plan; applying it twice would double-propose."""
        one = '{"tool": "propose_subtasks", "args": {"items": [{"label": "a"}]}}'
        block = f'```batonkeep-plan\n[{one},{one}]\n```'
        Maker, planner = self._cli_env(planner_env, monkeypatch, block)
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="grok"
        )
        async with Maker() as db:
            wi = await db.get(WorkItem, 1)
            assert len(wi.subtasks["items"]) == 1

    @pytest.mark.asyncio
    async def test_malformed_block_is_recorded_not_swallowed(self, planner_env, monkeypatch):
        Maker, planner = self._cli_env(
            planner_env, monkeypatch, "```batonkeep-plan\n[{oops,,}]\n```")
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="grok"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert "not valid JSON" in (run.error or "")

    @pytest.mark.asyncio
    async def test_api_lane_is_untouched_by_the_protocol(self, planner_env):
        """The API lane keeps native tool-calling; a stray block in its prose is
        not a second way to invoke tools."""
        Maker, planner = planner_env
        run_id = await planner.run_planning_turn(
            "pr", work_item_id=1, message="plan it", owner_id="local", provider="planner-mock"
        )
        async with Maker() as db:
            run = await db.get(PlannerRun, run_id)
            assert run.proposals["subtasks_proposed"] == 2   # from the tools, not text
