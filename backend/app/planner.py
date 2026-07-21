"""
planner.py — the per-project planner agent lane (P-0078).

Planning/meta-work is a distinct work class from execution: high-frequency,
lower-stakes, broad-context (decompose a WorkItem, propose its sub-task checklist,
keep `next_action` honest). Today it is an incidental side-effect of whichever
executor happens to run a build turn; this module gives it a home — a **lightweight
planning turn** that operates on Project + WorkItem DB state with a dedicated
planning toolset and **no workspace** (planning is DB-state meta-work, not
filesystem work, so it never spins a git workspace like a build session does).

It **reuses the seams**, it is not a new engine: the same `ModelExecutor` (run in
"planning mode" via `extra['planning']`, which fences the toolset to the planner
tools and swaps the system prompt), the same tool registry (P-0017), and the same
sovereignty fence as executors (P-0009 #1: a confidential project's planner is
pinned to a local model). The planner is **proposer-only** — it never approves
durable truth; its outputs land as proposals the operator confirms (the [[P-0069]]
B2 model).

Selection (per the founder's build-time call, 2026-07-21): a **fixed per-Project
default** provider/model (`Project.planner_provider`/`planner_model`), overridable
per call, falling back to the first available instance — a planner is never a
mandatory per-project decision.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import UTC, datetime

from app.db import AsyncSessionLocal
from app.models import PlannerRun, Project, WorkItem
from app.providers.base import EventKind, ExecResult
from app.providers.registry import (
    get_executor,
    is_local_instance,
    list_instances,
    local_candidate_ids,
)

logger = logging.getLogger(__name__)

# Planning is cheap, frequent meta-work — a tight budget/round cap keeps it so.
_PLANNER_BUDGET_USD = 0.50
_PLANNER_MAX_ROUNDS = 6


class PlannerError(Exception):
    """Caller-facing planner problem (no provider available / unknown project)."""


#: Hold strong refs to fire-and-forget drives so they aren't GC'd mid-flight.
_bg_tasks: set[asyncio.Task] = set()


def schedule_drive(run_id: int) -> None:
    """Fire-and-forget the background drive for a started planning turn."""
    task = asyncio.create_task(drive_planning_turn(run_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def resolve_planner_selection(
    project: Project,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
) -> tuple[str, str | None, bool]:
    """Resolve the planner provider/model for a project. Precedence: an explicit
    per-call request → the project's planner default → the first available instance.
    A confidential project pins to a local model (the P-0009 #1 sovereignty fence);
    returns `(provider, model, local_pinned)`. Raises PlannerError if nothing is
    available (or nothing *local* is, for a confidential project)."""
    provider = requested_provider or project.planner_provider
    model = requested_model or project.planner_model
    if not provider:
        instances = list_instances()
        if not instances:
            raise PlannerError("no provider is available to run the planner")
        provider = instances[0].id

    confidential = project.sensitivity == "confidential"
    local_pinned = False
    if confidential and not is_local_instance(provider):
        locals_ = local_candidate_ids()
        if not locals_:
            raise PlannerError(
                "this project is confidential but no local provider is available — "
                "configure a local model (e.g. ollama) to run its planner"
            )
        provider = locals_[0]
        model = None  # the remote model id doesn't apply to the local provider
        local_pinned = True
    elif confidential:
        local_pinned = True  # already local
    return provider, model, local_pinned


def _build_prompt(project: Project, work_item: WorkItem | None, message: str) -> str:
    """The planning turn's user prompt: project + work-item state so the planner
    reasons from durable truth, not a transcript. Content-safe (no raw evidence)."""
    from app import subtasks as st

    lines = [f"# Project: {project.name}"]
    if project.description:
        lines.append(project.description.strip()[:800])
    if work_item is not None:
        lines.append(f"\n## Work item #{work_item.id}: {work_item.title}")
        lines.append(f"State: {work_item.state} · kind: {work_item.kind} · risk: {work_item.risk}")
        if work_item.objective:
            lines.append(f"Objective: {work_item.objective.strip()[:1200]}")
        if work_item.next_action:
            lines.append(f"Current next action: {work_item.next_action.strip()[:400]}")
        existing = st._items(work_item.subtasks)
        if existing:
            lines.append("Existing sub-tasks (do not duplicate):")
            for i in existing:
                if i.get("verified"):
                    mark = "✓"
                elif i.get("status") == "confirmed":
                    mark = "·"
                else:
                    mark = "?"
                exp = f" → {i['expected']}" if i.get("expected") else ""
                lines.append(f"  {mark} {i.get('label')}{exp}")
    if message.strip():
        lines.append(f"\n## Operator note\n{message.strip()[:1000]}")
    lines.append(
        "\nPropose the sub-task plan (with expected artifacts where a sub-task makes a "
        "file) and set the single honest next action."
    )
    return "\n".join(lines)


async def start_planning_turn(
    project_id: str,
    *,
    work_item_id: int | None = None,
    message: str = "",
    owner_id: str = "local",
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Validate + resolve selection, persist a `running` PlannerRun, and return its
    id — fast, no executor drive. The caller schedules `drive_planning_turn(run_id)`
    as a background task (the lane is non-blocking, like task runs / session turns).
    Raises PlannerError on a bad project/work-item or no available provider."""
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if project is None or project.owner_id != owner_id:
            raise PlannerError("project not found")
        work_item: WorkItem | None = None
        if work_item_id is not None:
            work_item = await db.get(WorkItem, work_item_id)
            if work_item is None or work_item.owner_id != owner_id \
                    or work_item.project_id != project_id:
                raise PlannerError("work item not found in this project")
        prov, mdl, local_pinned = resolve_planner_selection(
            project, requested_provider=provider, requested_model=model
        )
        run = PlannerRun(
            owner_id=owner_id, project_id=project_id, work_item_id=work_item_id,
            status="running", provider=prov, model=mdl, local_pinned=local_pinned,
            request=_build_prompt(project, work_item, message),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def drive_planning_turn(run_id: int) -> None:
    """Drive a `running` PlannerRun to completion: run the executor in planning mode
    against the stored prompt, letting the planner tools land proposals on the work
    item, then finalize the row. Best-effort — a planner failure is recorded on the
    row, never raised (this runs as a fire-and-forget background task)."""
    async with AsyncSessionLocal() as db:
        run = await db.get(PlannerRun, run_id)
        if run is None or run.status != "running":
            return
        prov = run.provider or ""
        mdl = run.model
        prompt = run.request or ""
        owner_id, project_id, work_item_id = run.owner_id, run.project_id, run.work_item_id

    executor = get_executor(prov)
    if executor is None:
        await _finish(run_id, status="failed", error=f"provider {prov!r} is not available")
        return

    final_result: ExecResult | None = None
    error_msg: str | None = None
    with tempfile.TemporaryDirectory(prefix="planner-") as scratch:
        try:
            async for ev in executor.run_stream(
                prompt,
                workdir=scratch,  # unused by planning tools; a throwaway scratch dir
                tools_enabled=True,
                max_rounds=_PLANNER_MAX_ROUNDS,
                budget_usd=_PLANNER_BUDGET_USD,
                extra={
                    "planning": True,
                    "owner_id": owner_id,
                    "project_id": project_id,
                    "work_item_id": work_item_id,
                    "model": mdl,
                },
            ):
                if ev.kind == EventKind.result:
                    final_result = ev.data.get("result")
                elif ev.kind == EventKind.error:
                    error_msg = ev.message or "planner error"
                    break
        except Exception as exc:  # a planner crash is recorded, never propagated up
            logger.exception("[planner] run %d failed", run_id)
            error_msg = str(exc)

    if final_result is None:
        await _finish(run_id, status="failed", error=error_msg or "no result produced")
        return
    await _finish(
        run_id, status="succeeded", response=final_result.text,
        model=final_result.model, usage=final_result.usage,
        work_item_id=work_item_id,
    )


async def run_planning_turn(
    project_id: str,
    *,
    work_item_id: int | None = None,
    message: str = "",
    owner_id: str = "local",
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Synchronous convenience (used by tests): start + drive to completion in one
    call. Production callers use start_planning_turn + a background drive_planning_turn."""
    run_id = await start_planning_turn(
        project_id, work_item_id=work_item_id, message=message, owner_id=owner_id,
        provider=provider, model=model,
    )
    await drive_planning_turn(run_id)
    return run_id


async def _finish(
    run_id: int,
    *,
    status: str,
    response: str | None = None,
    error: str | None = None,
    model: str | None = None,
    usage=None,
    work_item_id: int | None = None,
) -> None:
    """Finalize the PlannerRun row + record what the turn proposed (a grounded
    roll-up read back from the work item, not the planner's self-report)."""
    from app import subtasks as st

    async with AsyncSessionLocal() as db:
        run = await db.get(PlannerRun, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = datetime.now(UTC)
        if response is not None:
            run.response = response
        if error is not None:
            run.error = error
        if model:
            run.model = model
        if usage is not None:
            run.tokens_in = usage.tokens_in
            run.tokens_out = usage.tokens_out
            run.cost_usd = usage.cost_usd
        if status == "succeeded" and work_item_id is not None:
            wi = await db.get(WorkItem, work_item_id)
            if wi is not None:
                prog = st.progress(wi.subtasks)
                run.proposals = {
                    "subtasks_proposed": prog["proposed"],
                    "subtasks_confirmed": prog["total"],
                    "next_action": (wi.next_action or "")[:400] or None,
                }
        await db.commit()
