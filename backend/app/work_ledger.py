"""
work_ledger.py — the deterministic working ledger (S0 substrate).

WORKITEM.md is the durable working memory an actor receives for a run/turn:
objective, state, decisions, pending approvals, changed files, the evidence
index, and the single honest next action. It is rendered from structured DB
fields only — never from transcripts — and is **byte-stable given equal
inputs**: that is the property `ContextReceipt.ledger_sha` pins, and what the
cross-provider cold-handoff proof relies on (a fresh provider continues from
projection + ledger alone).

One input is not a DB field: `undeclared_count` (P-0073) measures the
projection against the canonical root, because an actor must be told when its
view is a strict subset of the truth store. It is still *recorded* state — the
projection writes it to the receipt's exclusions — so the reproducibility
contract holds against the receipt, which is what a cold reproducer has.

Determinism rules: no wall-clock reads, no row timestamps (two identical work
items created at different times must hash identically), stored iteration
order only (decision `ts` values appear because they are stored facts, not
render-time state). Agents may *propose* ledger fields; the orchestrator
writes them — this module only renders what the DB already says.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from app.models import WorkItem

LEDGER_FILENAME = "WORKITEM.md"


def sha256_text(text: str) -> str:
    """sha256 hex of the exact ledger bytes (utf-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line(value: str | None, fallback: str) -> str:
    value = (value or "").strip()
    return value if value else fallback


def _decision_lines(decisions: Sequence[Mapping[str, Any]] | None) -> list[str]:
    lines = []
    for d in decisions or []:
        ts = str(d.get("ts", "") or "").strip()
        actor = str(d.get("actor", "") or "").strip()
        text = str(d.get("text", "") or "").strip()
        prefix = " — ".join(p for p in (ts, actor) if p)
        lines.append(f"- {prefix}: {text}" if prefix else f"- {text}")
    return lines or ["- (none)"]


def format_evidence_line(e: Mapping[str, Any]) -> str:
    """One deterministic index line per evidence row. The [WI-n]/[project]
    prefix + trailing evidence id let a cold operator locate a row's origin and
    pull it via the API/UI; the same lines are hashed into the receipt's
    `index_sha`, so this formatting is part of the receipt contract."""
    wi = e.get("work_item_id")
    prefix = f"[WI-{wi}]" if wi is not None else "[project]"
    line = (
        f"- {prefix} {e.get('kind', '?')}: {e.get('rel_path', '?')} "
        f"(sha256 {e.get('digest') or '—'})"
    )
    eid = e.get("evidence_id")
    return f"{line} · evidence {eid}" if eid is not None else line


def render_ledger(
    *,
    project_name: str,
    work_item: WorkItem | None,
    changed_files: Sequence[str] = (),
    evidence_index: Sequence[Mapping[str, Any]] = (),
    pending_approvals: Sequence[str] = (),
    pinned_inputs: Sequence[str] = (),
    undeclared_count: int = 0,
) -> str:
    """Render the working ledger. Pure function of its inputs — same inputs,
    same bytes (see module docstring for what must stay out of here)."""
    out: list[str] = []
    if work_item is not None:
        out.append(f"# Work ledger — {work_item.title}")
        out.append("")
        out.append(f"Project: {project_name}")
        out.append(f"Kind: {work_item.kind} · State: {work_item.state} · Risk: {work_item.risk}")
        out.append("")
        out.append("## Objective")
        out.append(_line(work_item.objective, "(no objective recorded)"))
        out.append("")
        out.append("## Decisions")
        out.extend(_decision_lines(work_item.decisions))
    else:
        # No work item attached: still emit a deterministic ledger so every
        # receipt carries a ledger_sha and the projection shape is uniform.
        out.append(f"# Work ledger — {project_name}")
        out.append("")
        out.append("No work item is attached to this run/turn. Durable intent lives in")
        out.append("the project's work items; this execution runs against the project only.")
    out.append("")
    out.append("## Pending approvals")
    out.extend([f"- {p}" for p in pending_approvals] or ["- (none)"])
    out.append("")
    out.append("## Changed files")
    out.extend([f"- {f}" for f in changed_files] or ["- (none)"])
    out.append("")
    # Materialized pinned evidence — the inputs a cold actor should read first;
    # the paths exist read-only inside this workspace.
    out.append("## Inputs (pinned evidence)")
    out.extend([f"- {p}" for p in pinned_inputs] or ["- (none)"])
    out.append("")
    out.append("## Evidence")
    out.extend(
        [format_evidence_line(e) for e in evidence_index] or ["- (none)"]
    )
    out.append("")
    # P-0073: only rendered when the projection is a strict subset of the
    # canonical root, so a fully-declared project's ledger keeps its exact
    # prior bytes. The closing instruction is the point: an actor whose view is
    # short must say so, not go hunting on disk for what it was briefed to read
    # (the pilot #43 chain that ended in a cross-session write).
    if undeclared_count > 0:
        out.append("## Context coverage")
        out.append(
            f"- INCOMPLETE: {undeclared_count} file(s) under this project's context "
            "root are not covered by any declared source, so they are NOT in your "
            "projection."
        )
        out.append(
            "- If your brief refers to material you cannot find under `context/`, "
            "this is why. Record what is missing and stop; do not search the "
            "filesystem for it."
        )
        out.append("")
    out.append("## Next action")
    out.append(
        _line(work_item.next_action if work_item is not None else None, "(none recorded)")
    )
    out.append("")
    return "\n".join(out)
