"""
providers/tools/planner_tools.py — the per-project planner agent's toolset (P-0078).

Planning is a distinct work class from execution: high-frequency, lower-stakes,
broad-context meta-work (decompose a WorkItem, propose its sub-task checklist, keep
`next_action` honest). These tools are the planner's hands. Unlike the build tools
(filesystem/code-exec, workdir-scoped), planning tools operate on **DB state** —
Project + WorkItem rows — so they take their scope from the run `context`
(`owner_id`, `project_id`, `work_item_id`) that the model executor threads through
`_call_tool`, not from a workspace. There is no workspace for a planning turn.

Guardrail — **proposer-only** (the founder-approval boundary, [[P-0078]]): the
planner never approves durable truth. `propose_subtasks` lands items as `proposed`
(the operator confirms/modifies via the B2 `PUT …/subtasks` path — the exact
[[P-0069]] B2 model); `set_next_action` writes the honest-next-step field, which the
data model already designates as agent-proposed / orchestrator-written operating
state (not founder-gated truth). The **structural** tools (slice 2) mint whole work
items — durable intent — so they stage their output the same way: a new work item
lands in the `proposed` state, which the operator accepts (→ `open`) or rejects
(→ `dropped`). `summarize_project` writes nothing durable at all; its digest is
derived state recorded on the planning turn's own audit row.

Scope: the item tools (`propose_subtasks`, `set_next_action`, `decompose`) need a
bound work item; the project tools (`triage_signal`, `summarize_project`) operate on
the project. The executor offers only the set matching the turn's scope, so the
model is never handed a tool that would just error.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import PlannerRun, Project, WorkItem

logger = logging.getLogger(__name__)

# The subtasks a single propose call may add — a guardrail against a runaway
# planner; the checklist itself caps at subtasks.MAX_ITEMS.
_MAX_PROPOSE = 25
_MAX_NEXT_ACTION = 2000
# Structural output is capped harder than checklist output: a work item is durable
# intent a human must review one by one, so a runaway decompose is expensive.
_MAX_CHILDREN = 10
_MAX_TITLE = 256
_MAX_OBJECTIVE = 4000
_VALID_RISKS = ("low", "medium", "high")


PROPOSE_SUBTASKS_SCHEMA = {
    "name": "propose_subtasks",
    "description": (
        "Propose sub-tasks for the current work item's output-contract checklist. "
        "Each sub-task is a concrete deliverable. If a sub-task produces a file, give "
        "its `expected` path or glob (e.g. 'report.md', 'charts/*.png') — that makes it "
        "auto-verifiable: it is marked done only when the artifact actually lands. Omit "
        "`expected` for a sub-task with no file artifact. Proposed sub-tasks await the "
        "operator's confirmation; you cannot mark them done yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The sub-tasks to propose.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "What this sub-task delivers."},
                        "expected": {
                            "type": "string",
                            "description": "Optional artifact path/glob (makes it verifiable).",
                        },
                    },
                    "required": ["label"],
                },
            },
        },
        "required": ["items"],
    },
}

SET_NEXT_ACTION_SCHEMA = {
    "name": "set_next_action",
    "description": (
        "Set the current work item's single honest next action — the one concrete step "
        "a cold operator or a fresh provider would take next. Keep it specific and short."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "next_action": {"type": "string", "description": "The single next step."},
        },
        "required": ["next_action"],
    },
}


DECOMPOSE_SCHEMA = {
    "name": "decompose",
    "description": (
        "Break the current work item into child work items — use this only when a piece "
        "is genuinely a separate unit of work (its own objective, its own lifecycle, "
        "possibly its own session). For steps within this work item, use propose_subtasks "
        "instead; that is the common case. Children are created in the `proposed` state "
        "and do nothing until the operator accepts them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The child work items to propose.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short name for the child."},
                        "objective": {
                            "type": "string",
                            "description": "What done means for this child, concretely.",
                        },
                        "kind": {
                            "type": "string",
                            "description": "task | investigation | review | chore (default task).",
                        },
                        "risk": {
                            "type": "string",
                            "enum": list(_VALID_RISKS),
                            "description": "Default low.",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["items"],
    },
}

TRIAGE_SIGNAL_SCHEMA = {
    "name": "triage_signal",
    "description": (
        "Turn something that needs doing in this project into a tracked work item — a "
        "gap you spotted in the ledger, or a note the operator handed you. Creates the "
        "item in the `proposed` state for the operator to accept or reject. Do not "
        "duplicate work items that already exist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short name for the work item."},
            "objective": {
                "type": "string",
                "description": "What done means for it, concretely.",
            },
            "kind": {
                "type": "string",
                "description": "task | incident | investigation | review (default task).",
            },
            "risk": {"type": "string", "enum": list(_VALID_RISKS), "description": "Default low."},
            "source": {
                "type": "string",
                "description": "Where this came from (e.g. 'operator note', 'gap in the ledger').",
            },
        },
        "required": ["title"],
    },
}

SUMMARIZE_PROJECT_SCHEMA = {
    "name": "summarize_project",
    "description": (
        "Record a status digest for this project: the one-line headline, which work "
        "items deserve attention now, and which look stalled. Reference work items by "
        "their numeric id. This is a read-out for the operator — it changes no work "
        "item state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One line: where this project actually stands.",
            },
            "focus": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Work item ids that deserve attention now.",
            },
            "stalled": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Work item ids that look stuck or forgotten.",
            },
            "notes": {"type": "string", "description": "Optional short elaboration."},
        },
        "required": ["headline"],
    },
}


def _ctx(context: dict | None) -> tuple[str, int | None]:
    """Owner + work-item scope for a planning tool call, from the run context."""
    ctx = context or {}
    owner_id = ctx.get("owner_id") or "local"
    work_item_id = ctx.get("work_item_id")
    return owner_id, work_item_id


def _project_ctx(context: dict | None) -> tuple[str, str | None, int | None]:
    """Owner + project + planner-run scope for a project-level planning tool call."""
    ctx = context or {}
    return (
        ctx.get("owner_id") or "local",
        ctx.get("project_id"),
        ctx.get("planner_run_id"),
    )


def _clean_risk(value) -> str:
    risk = (value or "").strip().lower() if isinstance(value, str) else ""
    return risk if risk in _VALID_RISKS else "low"


async def _record_proposed_items(db, run_id: int | None, owner_id: str, ids: list[int]) -> None:
    """Append work items this turn minted to its own audit row, so `proposals` is an
    exact record of what the turn produced rather than a timestamp-window guess.
    Best-effort: a planning turn is still valid if the roll-up cannot be attributed."""
    if not run_id or not ids:
        return
    run = await db.get(PlannerRun, run_id)
    if run is None or run.owner_id != owner_id:
        return
    prior = (run.proposals or {}).get("work_items_proposed") or []
    # Reassign (never mutate in place) so the JSON column change is tracked.
    run.proposals = {**(run.proposals or {}), "work_items_proposed": [*prior, *ids]}


async def propose_subtasks(items: list[dict], *, context: dict | None = None) -> str:
    """Append operator-review sub-tasks (status=proposed) to the bound work item's
    checklist. Proposer-only: the operator confirms/modifies them (B2 PUT path)."""
    from app import subtasks as st

    owner_id, work_item_id = _ctx(context)
    if not work_item_id:
        return "[propose_subtasks error] no work item is bound to this planning turn"
    raw = [i for i in (items or []) if isinstance(i, dict) and (i.get("label") or "").strip()]
    if not raw:
        return "[propose_subtasks error] no valid sub-tasks provided"
    if len(raw) > _MAX_PROPOSE:
        return f"[propose_subtasks error] at most {_MAX_PROPOSE} sub-tasks per call"
    async with AsyncSessionLocal() as db:
        wi = await db.get(WorkItem, work_item_id)
        if wi is None or wi.owner_id != owner_id:
            return "[propose_subtasks error] work item not found"
        try:
            wi.subtasks = st.append_proposed(wi.subtasks, raw, proposed_by="planner")
        except ValueError as exc:
            return f"[propose_subtasks error] {exc}"
        await db.commit()
        proposed = [i["label"] for i in st._items(wi.subtasks) if i.get("status") == "proposed"]
    return (
        f"[propose_subtasks] proposed {len(raw)} sub-task(s) for operator review "
        f"({len(proposed)} awaiting confirmation): {', '.join(i['label'] for i in raw)}"
    )


async def set_next_action(next_action: str, *, context: dict | None = None) -> str:
    """Write the bound work item's honest next action (agent-writable operating
    state — the model designates next_action as agent-proposed / engine-written)."""
    owner_id, work_item_id = _ctx(context)
    if not work_item_id:
        return "[set_next_action error] no work item is bound to this planning turn"
    text = (next_action or "").strip()[:_MAX_NEXT_ACTION]
    if not text:
        return "[set_next_action error] next_action must not be empty"
    async with AsyncSessionLocal() as db:
        wi = await db.get(WorkItem, work_item_id)
        if wi is None or wi.owner_id != owner_id:
            return "[set_next_action error] work item not found"
        wi.next_action = text
        await db.commit()
    return f"[set_next_action] next action set: {text}"


async def decompose(items: list[dict], *, context: dict | None = None) -> str:
    """Propose child work items under the bound work item. Structural: this mints
    durable intent, so children land `proposed` for the operator to accept/reject."""
    owner_id, work_item_id = _ctx(context)
    _, _, run_id = _project_ctx(context)
    if not work_item_id:
        return "[decompose error] no work item is bound to this planning turn"
    raw = [i for i in (items or []) if isinstance(i, dict) and (i.get("title") or "").strip()]
    if not raw:
        return "[decompose error] no valid child work items provided"
    if len(raw) > _MAX_CHILDREN:
        return f"[decompose error] at most {_MAX_CHILDREN} children per call"
    async with AsyncSessionLocal() as db:
        parent = await db.get(WorkItem, work_item_id)
        if parent is None or parent.owner_id != owner_id:
            return "[decompose error] work item not found"
        titles, children = [], []
        for spec in raw:
            title = spec["title"].strip()[:_MAX_TITLE]
            child = WorkItem(
                owner_id=owner_id,
                project_id=parent.project_id,
                parent_id=parent.id,
                state="proposed",
                kind=(spec.get("kind") or parent.kind or "task").strip()[:64] or "task",
                title=title,
                objective=(spec.get("objective") or "").strip()[:_MAX_OBJECTIVE],
                risk=_clean_risk(spec.get("risk")),
                signal={
                    "source": "planner",
                    "kind": "decompose",
                    "parent_id": parent.id,
                    "ts": datetime.now(UTC).isoformat(),
                },
            )
            db.add(child)
            children.append(child)
            titles.append(title)
        await db.flush()
        await _record_proposed_items(db, run_id, owner_id, [c.id for c in children])
        await db.commit()
    return (
        f"[decompose] proposed {len(titles)} child work item(s) under #{work_item_id}, "
        f"awaiting operator acceptance: {', '.join(titles)}"
    )


async def triage_signal(
    title: str,
    *,
    objective: str = "",
    kind: str = "task",
    risk: str = "low",
    source: str = "",
    context: dict | None = None,
) -> str:
    """Propose a top-level work item for this project from a signal or an observed
    gap. Lands `proposed` — the operator decides whether it becomes real work."""
    owner_id, project_id, run_id = _project_ctx(context)
    if not project_id:
        return "[triage_signal error] no project is bound to this planning turn"
    clean_title = (title or "").strip()[:_MAX_TITLE]
    if not clean_title:
        return "[triage_signal error] title must not be empty"
    async with AsyncSessionLocal() as db:
        # The run row's project was validated when the turn started, but these tools
        # are dispatched by name off model output — re-check ownership here so the
        # tool is safe on its own terms, not only via the lane that calls it today.
        project = await db.get(Project, project_id)
        if project is None or project.owner_id != owner_id:
            return "[triage_signal error] project not found"
        item = WorkItem(
            owner_id=owner_id,
            project_id=project_id,
            state="proposed",
            kind=(kind or "task").strip()[:64] or "task",
            title=clean_title,
            objective=(objective or "").strip()[:_MAX_OBJECTIVE],
            risk=_clean_risk(risk),
            signal={
                "source": (source or "planner triage").strip()[:200],
                "kind": "triage",
                "ts": datetime.now(UTC).isoformat(),
            },
        )
        db.add(item)
        await db.flush()
        item_id = item.id
        await _record_proposed_items(db, run_id, owner_id, [item_id])
        await db.commit()
    return (
        f"[triage_signal] proposed work item #{item_id} {clean_title!r} "
        f"for operator acceptance"
    )


async def summarize_project(
    headline: str,
    *,
    focus: list | None = None,
    stalled: list | None = None,
    notes: str = "",
    context: dict | None = None,
) -> str:
    """Record the planner's project status digest on this planning turn's audit row.
    Mutates no work item — a summary is derived state, not durable truth. Referenced
    ids are filtered against the project's real work items, so the digest cannot
    point at work that does not exist (grounded, not self-reported)."""
    owner_id, project_id, run_id = _project_ctx(context)
    if not project_id:
        return "[summarize_project error] no project is bound to this planning turn"
    if not run_id:
        return "[summarize_project error] no planning turn is bound to this call"
    line = (headline or "").strip()[:600]
    if not line:
        return "[summarize_project error] headline must not be empty"

    async with AsyncSessionLocal() as db:
        real_ids = set((await db.execute(
            select(WorkItem.id).where(
                WorkItem.owner_id == owner_id, WorkItem.project_id == project_id
            )
        )).scalars().all())

        def _ids(values) -> list[int]:
            out = []
            for v in values or []:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if n in real_ids and n not in out:
                    out.append(n)
            return out[:25]

        focus_ids, stalled_ids = _ids(focus), _ids(stalled)
        run = await db.get(PlannerRun, run_id)
        if run is None or run.owner_id != owner_id:
            return "[summarize_project error] planning turn not found"
        # Reassign (never mutate in place) so the JSON column change is tracked.
        run.proposals = {
            **(run.proposals or {}),
            "summary": {
                "headline": line,
                "focus": focus_ids,
                "stalled": stalled_ids,
                "notes": (notes or "").strip()[:2000] or None,
            },
        }
        await db.commit()
    dropped = len(focus or []) + len(stalled or []) - len(focus_ids) - len(stalled_ids)
    tail = f" ({dropped} unknown work item id(s) ignored)" if dropped > 0 else ""
    return (
        f"[summarize_project] digest recorded: {line} · focus={focus_ids} "
        f"stalled={stalled_ids}{tail}"
    )
