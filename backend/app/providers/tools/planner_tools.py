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
state (not founder-gated truth). Structural tools that would mint durable
intent (decompose into child work items, triage a signal into a work item) land in
a later slice and stage their output as proposals too.

This slice ships the two tools that ride the existing B2 substrate; decompose /
triage / summarize follow in slice 2.
"""
from __future__ import annotations

import logging

from app.db import AsyncSessionLocal
from app.models import WorkItem

logger = logging.getLogger(__name__)

# The subtasks a single propose call may add — a guardrail against a runaway
# planner; the checklist itself caps at subtasks.MAX_ITEMS.
_MAX_PROPOSE = 25
_MAX_NEXT_ACTION = 2000


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


def _ctx(context: dict | None) -> tuple[str, int | None]:
    """Owner + work-item scope for a planning tool call, from the run context."""
    ctx = context or {}
    owner_id = ctx.get("owner_id") or "local"
    work_item_id = ctx.get("work_item_id")
    return owner_id, work_item_id


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
