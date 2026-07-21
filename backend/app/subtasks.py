"""subtasks.py — WorkItem sub-task checklist = output contract + grounded progress
(P-0069 item 6, B2).

A WorkItem carries a lightweight checklist in `WorkItem.subtasks`
(`{"v":1,"items":[…]}`). Each item is either **verifiable** (it declares an
`expected` artifact path/glob) or **asserted** (no artifact). The checklist is the
opt-in **output contract**: a verifiable item is `done`+`verified` only when its
glob matches a file in the committed tree — so progress is derived from workspace
truth, not a self-reported counter (the P-0069 thesis). Asserted items can be
marked `done` but stay `verified=false` ("claimed, not verified"), so the honesty
line survives into the progress roll-up.

Declaration lifecycle (per [[P-0078]] the planner produces these; today an operator
or a build turn does): an item is `proposed` (awaiting confirm) → `confirmed`
(in-scope, counted) → `dropped` (rejected; kept only until the next set replaces
it). Verification and progress consider **confirmed** items only.

This module is pure (no DB/IO): the orchestrator/API own persistence and supply the
committed-tree file set. Kept DB-free so it is unit-testable without fixtures.
"""
from __future__ import annotations

import fnmatch
import uuid
from datetime import UTC, datetime
from typing import Any

SUBTASKS_VERSION = 1
MAX_ITEMS = 100
_MAX_LABEL = 200
_MAX_EXPECTED = 256
_VALID_STATUS = ("proposed", "confirmed", "dropped")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def make_item(
    label: str,
    *,
    expected: str | None = None,
    status: str = "proposed",
    proposed_by: str = "operator",
    item_id: str | None = None,
) -> dict[str, Any]:
    """One normalized checklist item. `expected` (path or glob) makes it verifiable;
    None makes it asserted. New items default to `proposed` (the P-0078 flow)."""
    label = (label or "").strip()[:_MAX_LABEL]
    if not label:
        raise ValueError("subtask label must not be empty")
    if status not in _VALID_STATUS:
        raise ValueError(f"subtask status must be one of {_VALID_STATUS}")
    exp = (expected or "").strip()[:_MAX_EXPECTED] or None
    return {
        "id": item_id or _new_id(),
        "label": label,
        "expected": exp,
        "status": status,
        "done": False,
        "verified": False,
        "verified_at": None,
        "verified_by": None,
        "proposed_by": (proposed_by or "operator")[:96],
    }


def empty() -> dict[str, Any]:
    return {"v": SUBTASKS_VERSION, "items": []}


def _items(subtasks: dict | None) -> list[dict]:
    if not subtasks or not isinstance(subtasks, dict):
        return []
    items = subtasks.get("items")
    return list(items) if isinstance(items, list) else []


def append_proposed(
    subtasks: dict | None, raw_items: list[dict], *, proposed_by: str
) -> dict[str, Any]:
    """Append agent/operator-proposed items (status=proposed). Raises on overflow."""
    existing = _items(subtasks)
    new = [
        make_item(
            r.get("label", ""), expected=r.get("expected"),
            status="proposed", proposed_by=proposed_by,
        )
        for r in raw_items
    ]
    if len(existing) + len(new) > MAX_ITEMS:
        raise ValueError(f"a work item may hold at most {MAX_ITEMS} sub-tasks")
    return {"v": SUBTASKS_VERSION, "items": existing + new}


def set_items(
    subtasks: dict | None, raw_items: list[dict], *, actor: str = "operator"
) -> dict[str, Any]:
    """Authoritative confirm/modify: replace the checklist with the operator's list,
    **preserving** verification state for an item whose id + expected are unchanged
    (so confirming a proposal doesn't discard a prior verification). New items (no id
    or unknown id) are minted; a changed `expected` resets verification."""
    prior = {i.get("id"): i for i in _items(subtasks) if isinstance(i, dict)}
    if len(raw_items) > MAX_ITEMS:
        raise ValueError(f"a work item may hold at most {MAX_ITEMS} sub-tasks")
    out: list[dict] = []
    for r in raw_items:
        status = r.get("status", "confirmed")
        item = make_item(
            r.get("label", ""), expected=r.get("expected"),
            status=status if status in _VALID_STATUS else "confirmed",
            proposed_by=r.get("proposed_by") or actor,
            item_id=r.get("id") or None,
        )
        old = prior.get(item["id"])
        if old and old.get("expected") == item["expected"]:
            # carry verification forward when the target is unchanged
            item["verified"] = bool(old.get("verified"))
            item["verified_at"] = old.get("verified_at")
            item["verified_by"] = old.get("verified_by")
            item["done"] = bool(old.get("done"))
        # an asserted item may be explicitly marked done by the operator
        if item["expected"] is None and bool(r.get("done")):
            item["done"] = True
        out.append(item)
    return {"v": SUBTASKS_VERSION, "items": out}


def verify(
    subtasks: dict | None,
    tracked: set[str],
    *,
    source: dict | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Re-verify confirmed **verifiable** items against the committed tree. A verifiable
    item is done+verified iff its `expected` glob matches a tracked file. Idempotent;
    returns (updated_subtasks_or_None, changed). Asserted items are untouched.

    `source` (optional) stamps provenance on an item that *newly* verifies:
    `verified_by = {lane, ref, seq?, at}`. Because a WorkItem may be worked across
    several sessions/runs whose per-lane turn `seq` both start at 1, `seq` alone is
    ambiguous WorkItem-wide; the globally-unique `ref` (session id / `run:<id>`)
    disambiguates which unit of work grounded each verification (P-0069 tail)."""
    items = _items(subtasks)
    if not items:
        return subtasks, False
    changed = False
    stamp = _verified_by(source) if source else None
    out: list[dict] = []
    for i in items:
        item = dict(i)
        exp = item.get("expected")
        if item.get("status") == "confirmed" and exp:
            hit = any(fnmatch.fnmatch(p, exp) or p == exp for p in tracked)
            if hit and not item.get("verified"):
                item["verified"] = True
                item["done"] = True
                item["verified_at"] = _now()
                item["verified_by"] = stamp
                changed = True
            elif not hit and item.get("verified"):
                # the artifact disappeared (e.g. reverted) — reflect truth
                item["verified"] = False
                item["done"] = False
                item["verified_at"] = None
                item["verified_by"] = None
                changed = True
        out.append(item)
    if not changed:
        return subtasks, False
    return {"v": SUBTASKS_VERSION, "items": out}, True


def _verified_by(source: dict) -> dict[str, Any]:
    """Normalize a verification-provenance stamp. `lane` ("session"|"task") + a
    globally-unique `ref` are the disambiguators; `seq` is kept for display only."""
    stamp: dict[str, Any] = {
        "lane": str(source.get("lane") or "")[:16] or "unknown",
        "ref": str(source.get("ref") or "")[:96],
        "at": _now(),
    }
    if source.get("seq") is not None:
        stamp["seq"] = source["seq"]
    return stamp


def unverified_verifiable(subtasks: dict | None) -> list[dict[str, Any]]:
    """Confirmed **verifiable** items whose artifact is not (yet) present — the
    contract's still-open obligations. Used to derive the `outputs_missing` advisory:
    a succeeded turn/run that committed work yet left these unmet (P-0069 tail)."""
    return [
        {"id": i.get("id"), "label": i.get("label"), "expected": i.get("expected")}
        for i in _items(subtasks)
        if i.get("status") == "confirmed" and i.get("expected") and not i.get("verified")
    ]


def progress(subtasks: dict | None) -> dict[str, int]:
    """Grounded roll-up over **confirmed** items: total, verified (artifact-backed),
    claimed (done but asserted/unverified), done (verified+claimed), and how many are
    still proposed (awaiting confirmation)."""
    items = _items(subtasks)
    confirmed = [i for i in items if i.get("status") == "confirmed"]
    verified = sum(1 for i in confirmed if i.get("verified"))
    claimed = sum(1 for i in confirmed if i.get("done") and not i.get("verified"))
    return {
        "total": len(confirmed),
        "verified": verified,
        "claimed": claimed,
        "done": verified + claimed,
        "proposed": sum(1 for i in items if i.get("status") == "proposed"),
    }
