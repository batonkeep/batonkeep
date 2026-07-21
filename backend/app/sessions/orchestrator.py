"""
sessions/orchestrator.py — build-session turn lifecycle (M1.1).

run_turn(session_id, message, provider?):
  1. Resolve the provider (explicit arg overrides; else the session's current one).
     Switching it routes THIS turn to a different executor and updates the session.
  2. Create a SessionTurn(status=running); broadcast.
  3. Build the turn prompt from the WORKSPACE (SESSION.md + file list + message) —
     not a replayed transcript (D-0008), so a switched-in agent continues cleanly.
  4. Stream executor events live over WS (reusing the run-event stream shape).
  5. On result: persist response, refresh the SESSION.md brief; status=succeeded.

After the turn's edits land, the orchestrator commits the workspace as a
**version** (M1.3, engine-owned commit boundary) and broadcasts the per-turn
diff to the live event view. Restore/diff of versions is served by the sessions
API (see main.py /versions, /diff, /restore).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas import SessionTurnOut

from sqlalchemy import func, select

from app import approvals
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.logging_config import owner_id_var, session_id_var
from app.models import Session, SessionTurn, WorkItem
from app.policy import resolve_effective_policy
from app.providers.base import EventKind, ExecResult
from app.providers.registry import (
    get_executor,
    is_local_instance,
    local_candidate_ids,
)
from app.redact import redact_text
from app.sessions import workspace as ws
from app.sessions.preview import (
    flag_unbacked_file_claims,
    rewrite_workspace_file_links,
)
from app.ws import ws_manager

logger = logging.getLogger(__name__)
_settings = get_settings()

# Per-turn spend cap applied when a session has no explicit budget set — the prior
# always-on default. A session budget (opt-in) replaces this with cumulative-remaining.
_DEFAULT_TURN_BUDGET_USD = 1.0

# In-flight turn background tasks, keyed by turn_id, so an interrupt can cancel the
# running agent (P-0057/D-0051). Mirrors the orchestrator's _cancel_handles for task
# runs. Cancelling the task raises CancelledError into run_turn_background's stream
# loop; the executor's `finally: proc.kill()` terminates the underlying CLI process
# (and the API-lane stream is aborted), so the interrupt is best-effort but real.
_turn_cancel_handles: dict[int, asyncio.Task] = {}


def dispatch_turn(turn_id: int, session_id: str, *, owner_id: str = "local") -> asyncio.Task:
    """Fire-and-forget run_turn_background as a tracked task so it can be cancelled.

    Returns the asyncio.Task. Registers it in _turn_cancel_handles and clears the
    handle on completion. Use this instead of a bare asyncio.ensure_future so an
    interrupt (cancel_turn) has a handle to cancel.
    """
    bg_task = asyncio.ensure_future(
        run_turn_background(turn_id, session_id, owner_id=owner_id)
    )
    _turn_cancel_handles[turn_id] = bg_task

    def _on_done(t: asyncio.Task) -> None:
        _turn_cancel_handles.pop(turn_id, None)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("[session] turn %d background task failed: %r", turn_id, exc)

    bg_task.add_done_callback(_on_done)
    return bg_task


async def cancel_turn(turn_id: int, session_id: str, *, owner_id: str = "local") -> bool:
    """Best-effort interrupt of an in-flight turn (P-0057/D-0051).

    Cancels the background task if it is still running; run_turn_background's
    CancelledError handler persists the partial output and marks the turn
    "cancelled". Returns True if a running turn was signalled, else False.
    """
    task = _turn_cancel_handles.get(turn_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


class SessionError(Exception):
    """Raised for caller-facing problems (unknown session/provider)."""


def enforce_local_if_confidential(chosen: str, confidential: bool) -> str:
    """Sovereignty boundary (P-0009 #1): a confidential session may only run on a
    local model. A remote selection is overridden to an available local provider;
    if none is available the turn fails closed rather than leaking off-box."""
    if not confidential or is_local_instance(chosen):
        return chosen
    locals_ = local_candidate_ids()
    if not locals_:
        raise SessionError(
            "this session is confidential but no local provider is available — "
            "configure a local model (e.g. ollama) to run it"
        )
    return locals_[0]


async def reap_orphaned_turns() -> int:
    """Reconcile build-session turns stranded by a backend restart (P-0057/D-0051).

    Turns execute as fire-and-forget asyncio tasks (dispatch_turn), so a crash/restart
    leaves any `running` turn with no executor — it would sit non-terminal forever and,
    after the interrupt feature, show a phantom Stop button that can't cancel anything.
    On startup we mark these `failed` with a clear reason so the state is honest. Mirror
    of orchestrator.reap_orphaned_runs for task runs.
    """
    now = datetime.now(UTC)
    reaped = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SessionTurn).where(SessionTurn.status == "running")
        )
        for turn in result.scalars().all():
            turn.status = "failed"
            turn.error = "interrupted by backend restart (reaped at startup)"
            turn.finished_at = now
            reaped += 1
        if reaped:
            await db.commit()
    if reaped:
        logger.warning("[session] reaped %d orphaned turn(s) on startup", reaped)
    return reaped


async def create_turn_record(
    session_id: str,
    message: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    owner_id: str = "local",
) -> tuple[int, SessionTurnOut]:
    """
    Validate inputs and create the SessionTurn DB row. Returns (turn_id, turn_out)
    immediately — does NOT run the agent. Raises SessionError for invalid inputs.

    Call run_turn_background(turn_id, session_id) as a background task to execute
    the turn without blocking the HTTP response (the 504-fix path).
    """
    from app.schemas import SessionTurnOut

    session_id_var.set(session_id)
    owner_id_var.set(owner_id)
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None or session.owner_id != owner_id:
            raise SessionError(f"session {session_id} not found")

        chosen = provider or session.provider
        if not chosen:
            raise SessionError("no provider selected for this session")
        policy = resolve_effective_policy(session=session)  # D-0058 seam 1
        chosen = enforce_local_if_confidential(chosen, policy.confidential)

        executor = get_executor(chosen)
        if executor is None:
            raise SessionError(f"provider {chosen!r} is not available")

        switched = chosen != session.provider
        session.provider = chosen
        # Per-session model override (P-0049, API path): "" clears back to the
        # provider's catalog default; a model id pins it; None leaves it unchanged.
        if model is not None:
            session.model = model.strip() or None

        seq = await _next_turn_seq(db, session_id)
        turn = SessionTurn(
            session_id=session_id,
            owner_id=owner_id,
            seq=seq,
            provider=chosen,
            prompt=message,
            status="running",
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        turn_out = SessionTurnOut.model_validate(turn)
        turn_id = turn.id

    # Broadcast the "running" state immediately so the UI shows the turn in-flight.
    await _broadcast_turn(turn_id, session_id, seq, chosen, "running",
                          extra={"switched": switched, "message": message})
    if switched:
        await _broadcast_event(
            session_id, turn_id, seq, EventKind.route,
            message=f"switched to {chosen}; continuing from workspace + SESSION.md",
        )

    return turn_id, turn_out


async def _snapshot_interrupted_turn(
    *,
    turn_id: int,
    session_id: str,
    seq: int,
    provider: str,
    workspace: str,
    owner_id: str,
    partial_text: str,
    reason: str,  # "cancelled" (user interrupt) | "timeout" (session-turn limit)
) -> None:
    """Capture the workspace state of a turn interrupted mid-flight, before its
    executor teardown discards it (P-0069 items 2+3+4b).

    Applies the D-0008 engine-owns-the-commit-boundary principle to interruption:
    whatever partial edits the agent wrote are committed as a version, recorded in
    the ledger, and (when the session is project-scoped) indexed as `partial-diff`
    evidence — labeled distinct from a completed turn's `diff` so salvage and
    forensics can tell partial from done. A kill-event is broadcast so an auditor
    can prove exactly what died and when (timestamp, files that landed, last file
    touch). Best-effort throughout: a failure here must never mask the interrupt.
    Sets the turn's commit fields; the caller owns the terminal status."""
    label = "cancelled" if reason == "cancelled" else "timed out"
    version: dict | None = None
    try:
        version = await ws.commit_snapshot(
            workspace, message=f"turn {seq} ({provider}): {label} — partial snapshot"
        )
    except Exception:
        logger.exception("[session] turn %d %s-snapshot commit failed", turn_id, reason)

    # Kill-event forensics: the last file the agent touched before it died. git
    # preserves working-tree mtimes across the snapshot commit, so this reflects
    # the agent's real last write, not the commit time.
    last_mtime: str | None = None
    files = version.get("files") if version else None
    if files:
        try:
            mtimes: list[float] = []
            for f in files:
                rel = f.get("path") if isinstance(f, dict) else f
                if not rel:
                    continue
                p = os.path.join(workspace, str(rel))
                if os.path.exists(p):
                    mtimes.append(os.path.getmtime(p))
            if mtimes:
                last_mtime = datetime.fromtimestamp(max(mtimes), UTC).isoformat()
        except Exception:
            logger.exception("[session] turn %d kill-event mtime scan failed", turn_id)

    if version is not None:
        try:
            ws.record_turn(
                workspace, seq=seq, provider=provider,
                summary=f"[{label}] {partial_text}", files=version.get("files"),
                lane="chat",
            )
        except Exception:
            logger.exception("[session] turn %d ledger record on %s failed", turn_id, reason)

    async with AsyncSessionLocal() as db:
        turn = await db.get(SessionTurn, turn_id)
        if turn is not None and version is not None:
            turn.commit_sha = version["commit"]
            turn.diffstat = version["diffstat"]
            turn.changed_files = json.dumps(version.get("files", []))
        session = await db.get(Session, session_id)
        if (
            session is not None and version is not None
            and session.project_id and version.get("diff")
        ):
            from app import evidence as evidence_store
            await evidence_store.capture_safe(
                db,
                owner_id=owner_id,
                project_id=session.project_id,
                work_item_id=session.work_item_id,
                session_turn_id=turn_id,
                kind="partial-diff",
                filename=f"turn_{seq}.partial.diff",
                text=version["diff"],
                producer=provider,
            )
        await db.commit()

    await _broadcast_event(
        session_id, turn_id, seq, EventKind.result,
        message=(
            f"{label}: partial snapshot {version['short']} committed"
            if version else f"{label}: no file changes to snapshot"
        ),
        phase="kill_event",
        data={
            "reason": reason,
            "at": datetime.now(UTC).isoformat(),
            "commit": version["commit"] if version else None,
            "short": version["short"] if version else None,
            "diffstat": version["diffstat"] if version else None,
            "files": version.get("files", []) if version else [],
            "last_file_mtime": last_mtime,
        },
    )


async def run_turn_background(
    turn_id: int,
    session_id: str,
    *,
    owner_id: str = "local",
) -> None:
    """
    Execute the agent for an already-created turn (created by create_turn_record).
    Streams events over WS, persists the result, commits the workspace version.
    Safe to run as a fire-and-forget asyncio task.
    """
    session_id_var.set(session_id)
    owner_id_var.set(owner_id)

    # Re-read the turn + session to get workspace path, provider, message etc.
    async with AsyncSessionLocal() as db:
        turn = await db.get(SessionTurn, turn_id)
        if turn is None:
            logger.error("[session] run_turn_background: turn %d not found", turn_id)
            return
        seq = turn.seq
        chosen = turn.provider
        message = turn.prompt
        session = await db.get(Session, session_id)
        if session is None:
            logger.error("[session] run_turn_background: session %s not found", session_id)
            return
        workspace = session.workspace_path
        # Effective declared policy for this turn (D-0058 seam 1): constraint
        # reads go through the one resolver so Phase C PolicySet inheritance is
        # an implementation swap, not a call-site rewrite.
        policy = resolve_effective_policy(session=session)
        exec_policy = policy.exec_policy
        image_model_id = session.image_model_id  # P-0046 slice 6: image-gen override
        model_override = session.model  # P-0049: per-session API model override
        # Load the immediate dialogue tail so conversational follow-ups keep their
        # referent (the workspace stays the source of truth — D-0008). Prior
        # completed turns only, most recent few, in chronological order.
        prior = (await db.execute(
            select(SessionTurn.prompt, SessionTurn.response)
            .where(
                SessionTurn.session_id == session_id,
                SessionTurn.seq < seq,
                SessionTurn.status == "succeeded",
            )
            .order_by(SessionTurn.seq.desc())
            .limit(ws.RECENT_TURNS)
        )).all()
        recent_turns = [(p, r or "") for p, r in reversed(prior)]

        # Per-session budget (opt-in). When set, the executor's budget gate caps
        # *cumulative* session spend, not just this one turn: pass the remaining
        # session headroom (cap − spend on prior succeeded turns). Composes with the
        # owner daily cap — whichever leaves less headroom wins. Unset → keep the
        # existing per-turn safety cap. Enforcement is stop-at-next-step (bounded
        # overshoot ≤ one round), surfaced as such in the UI.
        turn_budget = _DEFAULT_TURN_BUDGET_USD
        if policy.budget_cap_usd is not None:
            prior_spend = float((await db.execute(
                select(func.coalesce(func.sum(SessionTurn.cost_usd), 0.0))
                .where(
                    SessionTurn.session_id == session_id,
                    SessionTurn.seq < seq,
                    SessionTurn.status == "succeeded",
                )
            )).scalar() or 0.0)
            turn_budget = max(0.0, policy.budget_cap_usd - prior_spend)
        daily_cap = _settings.daily_budget_usd
        if daily_cap > 0:
            from app.cost import _start_of_today, spend_since
            daily_remaining = max(0.0, daily_cap - await spend_since(
                db, owner_id, _start_of_today()))
            turn_budget = min(turn_budget, daily_remaining)

        # S0 substrate: refresh the read-only `context/` projection + working
        # ledger in the workspace and persist the ContextReceipt before the
        # executor starts (the receipt must survive a crashed turn). The previous
        # turn's changed-files list feeds the ledger; failures are loud but never
        # block the turn.
        try:
            from app import project_context

            last_changed = (await db.execute(
                select(SessionTurn.changed_files)
                .where(
                    SessionTurn.session_id == session_id,
                    SessionTurn.seq < seq,
                    SessionTurn.status == "succeeded",
                    SessionTurn.changed_files.is_not(None),
                )
                .order_by(SessionTurn.seq.desc())
                .limit(1)
            )).scalar_one_or_none()
            changed: list[str] = []
            if last_changed:
                changed = [
                    str(f.get("path", "?")) if isinstance(f, dict) else str(f)
                    for f in json.loads(last_changed)
                ]
            receipt = await project_context.project_for_execution(
                db,
                owner_id=owner_id,
                project_id=session.project_id,
                work_item_id=session.work_item_id,
                workdir=workspace,
                session_turn_id=turn_id,
                changed_files=changed,
            )
            # Provenance stamp: the executing CLI version (API-lane executors
            # have no probe; the receipt keeps harness_version either way).
            if receipt is not None:
                probe = getattr(get_executor(chosen), "cli_version", None)
                version = probe() if callable(probe) else None
                if version:
                    receipt.cli_version = version
                    await db.commit()
        except Exception:
            logger.exception(
                "[session] turn %d context projection failed — continuing", turn_id
            )

    # Ledger pre-switch summary (best-effort, only if provider changed — we detect by
    # comparing to the previous turn's provider; approximate but good enough here).
    if _settings.ledger_summary_enabled:
        try:
            from app.sessions.ledger import summarize_session
            await summarize_session(session_id, owner_id=owner_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[session] pre-turn ledger summary failed: %s", exc)

    executor = get_executor(chosen)
    if executor is None:
        logger.error("[session] run_turn_background: executor for %s not found", chosen)
        async with AsyncSessionLocal() as db:
            t = await db.get(SessionTurn, turn_id)
            if t:
                t.status = "failed"
                t.error = f"provider {chosen!r} no longer available"
                t.finished_at = datetime.now(UTC)
                await db.commit()
        await _broadcast_turn(turn_id, session_id, seq, chosen, "failed")
        return

    # Build context from the workspace + a short dialogue tail (D-0008): the
    # workspace is the source of truth; the recent turns let follow-ups resolve.
    prompt = ws.build_turn_context(workspace, message, recent_turns=recent_turns)

    final_result: ExecResult | None = None
    error_msg: str | None = None
    # Accumulate streamed token text so an interrupt (P-0057/D-0051) can persist
    # whatever the agent produced before it was stopped.
    partial_chunks: list[str] = []

    async def _approve(code: str, label: str | None) -> bool:
        """P-0046 slice 3b: drive the code-exec `confirmation` round-trip — emit an
        approval-request event to the live view, then await the operator's decision
        (POST /api/sessions/{id}/approvals/{rid}; timeout → denied).

        The Future is the wakeup; the Approval row is the durable record
        (persisted best-effort — a DB hiccup must never wedge the turn)."""
        request_id, fut = approvals.request()
        try:
            async with AsyncSessionLocal() as adb:
                await approvals.record_request(
                    adb, owner_id=owner_id, request_id=request_id, kind="code_exec",
                    payload={"v": 1, "code": redact_text(code), "label": label},
                    producer=chosen, session_id=session_id,
                )
                await adb.commit()
        except Exception:
            logger.exception("[approvals] could not persist code_exec request row")
        await _broadcast_event(
            session_id, turn_id, seq, EventKind.approval,
            message=label or "Approve code execution?",
            data={"request_id": request_id, "code": code, "label": label, "tool": "code_exec"},
        )
        approved = await approvals.await_decision(request_id, fut)
        try:
            async with AsyncSessionLocal() as adb:
                # A timeout has no API decision; record it as denied by timeout.
                await approvals.settle(
                    adb, request_id, approved=approved,
                    decided_by="human" if approvals.was_resolved(request_id) else "timeout",
                )
                await adb.commit()
        except Exception:
            logger.exception("[approvals] could not settle code_exec request row")
        await _broadcast_event(
            session_id, turn_id, seq, EventKind.approval,
            message="approved" if approved else "denied",
            data={"request_id": request_id, "resolved": True, "approved": approved},
        )
        return approved

    # Session-turn timeout (P-0069 item 4): bound the whole turn's wall-clock drive
    # so a hung agent can't run unbounded (the dogfood's 81-min silent Agy handoff).
    # A user interrupt (cancel_turn → task.cancel) surfaces here as CancelledError;
    # the timeout's own expiry is converted to TimeoutError by the context manager's
    # __aexit__ (it only converts when its deadline fired) — so the two handlers
    # below stay cleanly distinct. Value is the effective declared policy timeout
    # (D-0058 seam 1; session default = run_timeout_seconds, 1800s), same bound the
    # task lane already enforces.
    effective_timeout = policy.timeout_seconds
    try:
        async with asyncio.timeout(effective_timeout):
            async for ev in executor.run_stream(
                prompt,
                workdir=workspace,
                tools_enabled=_settings.autonomous_tools,
                max_rounds=10,
                budget_usd=turn_budget,
                extra={
                    "session": True, "turn_seq": seq, "user_message": message,
                    # P-0046: interactive build sessions have a human in the loop; the
                    # session's policy gates whether code-exec is offered/runnable, and
                    # `confirmation` drives an approval round-trip via `_approve`.
                    "exec_policy": exec_policy, "human_in_loop": True, "approve": _approve,
                    # P-0046 slice 6: image-gen model override (None → provider default).
                    "image_model_id": image_model_id,
                    # P-0049: per-session model override for the API provider (None →
                    # the provider's catalog preferred.default).
                    "model": model_override,
                },
            ):
                # Rewrite agent file:// links to the raw-file route so the result
                # renders with clickable artifacts in the live view (P-0016 b).
                ev_text = ev.text
                if ev.kind == EventKind.result and ev_text:
                    ev_text = rewrite_workspace_file_links(ev_text, session_id, workspace)
                elif ev.kind == EventKind.token and ev.text:
                    partial_chunks.append(ev.text)
                await _broadcast_event(
                    session_id, turn_id, seq, ev.kind,
                    message=ev.message, text=ev_text, phase=ev.phase, data=ev.data)
                if ev.kind == EventKind.result:
                    final_result = ev.data.get("result")
                elif ev.kind == EventKind.error:
                    error_msg = ev.message or "executor error"
                    break
    except asyncio.CancelledError:
        # User interrupt (P-0057/D-0051): best-effort cancel. The executor's
        # `finally: proc.kill()` has terminated the underlying CLI/API work.
        # Snapshot whatever partial edits landed before teardown (P-0069 items 2+3:
        # cancel = snapshot — engine owns the commit boundary, D-0008), persist the
        # streamed partial as the response, mark the turn "cancelled", and re-raise
        # so the task ends cancelled.
        partial = "".join(partial_chunks).strip()
        logger.info("[session] turn %d cancelled by user (interrupt)", turn_id)
        await _snapshot_interrupted_turn(
            turn_id=turn_id, session_id=session_id, seq=seq, provider=chosen,
            workspace=workspace, owner_id=owner_id, partial_text=partial,
            reason="cancelled",
        )
        async with AsyncSessionLocal() as db:
            t = await db.get(SessionTurn, turn_id)
            if t is not None and t.status == "running":
                t.status = "cancelled"
                t.response = (
                    rewrite_workspace_file_links(partial, session_id, workspace)
                    if partial else None
                )
                t.finished_at = datetime.now(UTC)
                await db.commit()
        await _broadcast_turn(turn_id, session_id, seq, chosen, "cancelled",
                              extra={"partial": bool(partial)})
        raise
    except TimeoutError:
        # Session-turn timeout (P-0069 item 4): the 81-min silent-hang class. The
        # context manager cancelled the stream (executor teardown killed the CLI) and
        # surfaced expiry here. Snapshot the partial work (same principle as cancel),
        # then fail LOUDLY with a timeout error distinct from a user cancel.
        partial = "".join(partial_chunks).strip()
        minutes = effective_timeout / 60
        logger.warning(
            "[session] turn %d timed out after %ds (%.1f min)",
            turn_id, effective_timeout, minutes,
        )
        await _snapshot_interrupted_turn(
            turn_id=turn_id, session_id=session_id, seq=seq, provider=chosen,
            workspace=workspace, owner_id=owner_id, partial_text=partial,
            reason="timeout",
        )
        async with AsyncSessionLocal() as db:
            t = await db.get(SessionTurn, turn_id)
            if t is not None and t.status == "running":
                t.status = "failed"
                t.error = f"turn timed out after {minutes:g} min (session-turn limit)"
                t.response = (
                    rewrite_workspace_file_links(partial, session_id, workspace)
                    if partial else None
                )
                t.finished_at = datetime.now(UTC)
                await db.commit()
        await _broadcast_turn(turn_id, session_id, seq, chosen, "failed",
                              extra={"timeout": True, "partial": bool(partial)})
        return
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("[session] turn %d executor error", turn_id)
        error_msg = str(exc)

    response_text = final_result.text if final_result else ""
    if response_text:
        # Persist with file:// links rewritten so reloads also get clickable
        # artifacts, not just the live stream (P-0016 b).
        response_text = rewrite_workspace_file_links(response_text, session_id, workspace)
    version: dict | None = None
    if final_result is not None:
        # Engine owns the commit boundary (D-0008): commit the workspace as a
        # version once the turn's edits have landed. None if nothing changed.
        version = await ws.commit_turn(
            workspace, seq=seq, provider=chosen, summary=response_text
        )
        # Structured ledger entry grounded in the turn's artifacts (D-0017 thread 1).
        ws.record_turn(
            workspace, seq=seq, provider=chosen, summary=response_text,
            files=(version["files"] if version else None), lane="chat",
        )

    # After a successful turn, read the committed tree once — shared by the free
    # default output check (item 6) and the sub-task contract verification (B2).
    tracked: set[str] = set()
    output_flags: dict | None = None
    if final_result is not None:
        try:
            tracked = await ws.tracked_files(workspace)
        except Exception:
            logger.exception("[session] turn %d tracked-files read failed", turn_id)
        # Free default output check: flag response artifact links not backed by this
        # session's committed tree — the P43-D3 misdirected-writes tell, made
        # machine-checkable. Advisory (the turn still succeeds). Best-effort.
        try:
            unbacked = (
                flag_unbacked_file_claims(response_text, session_id, tracked)
                if response_text else []
            )
            if unbacked:
                output_flags = {"v": 1, "unbacked": unbacked}
                logger.warning(
                    "[session] turn %d: %d referenced file(s) not in committed tree: %s",
                    turn_id, len(unbacked), unbacked,
                )
                await _broadcast_event(
                    session_id, turn_id, seq, EventKind.log,
                    message=(
                        f"{len(unbacked)} referenced file(s) are not in this session's "
                        "committed tree — the work may have landed elsewhere"
                    ),
                    phase="outputs_check",
                    data={"unbacked": unbacked},
                )
        except Exception:
            logger.exception("[session] turn %d output-check failed", turn_id)

    if version is not None:
        # Surface the per-turn diff in the live event view (M1.3 Verify gate).
        await _broadcast_event(
            session_id, turn_id, seq, EventKind.result,
            message=f"version {version['short']} committed",
            phase="version",
            data={"commit": version["commit"], "short": version["short"],
                  "diffstat": version["diffstat"], "diff": version["diff"],
                  "files": version.get("files", [])},
        )

    async with AsyncSessionLocal() as db:
        turn = await db.get(SessionTurn, turn_id)
        if turn is not None:
            turn.finished_at = datetime.now(UTC)
            if final_result is not None:
                turn.status = "succeeded"
                turn.response = response_text
                # The model that ran this turn (P-0049) — feeds catalog usage metrics.
                turn.model = final_result.model
                # Persist token/cost usage so build-session spend shows in Analytics
                # (previously always $0 — the executor reports usage but it was dropped).
                usage = final_result.usage
                turn.tokens_in = usage.tokens_in
                turn.tokens_out = usage.tokens_out
                turn.cost_usd = usage.cost_usd
                turn.cache_read_tokens = usage.cache_read_tokens
                turn.cache_write_tokens = usage.cache_write_tokens
            else:
                turn.status = "failed"
                # error text often embeds provider stderr (A6 secrets wall)
                turn.error = redact_text(error_msg) if error_msg else "no result produced"
            if version is not None:
                turn.commit_sha = version["commit"]
                turn.diffstat = version["diffstat"]
                # Persist the per-file artifact list so reloads surface the same
                # result (D-0017 thread 2), not just the live event stream.
                turn.changed_files = json.dumps(version.get("files", []))
            # P-0069 item 6: durable free-default output flag (NULL when clean).
            turn.output_flags = output_flags
            await db.commit()
        session = await db.get(Session, session_id)
        if session is not None:
            session.updated_at = datetime.now(UTC)
            # B2 (P-0069 item 6): re-verify the bound work item's sub-task contract
            # against the committed tree — a verifiable item flips done+verified only
            # when its expected artifact actually landed here, so progress derives
            # from workspace truth, not a self-report. Best-effort.
            if final_result is not None and session.work_item_id:
                try:
                    from app import subtasks as st
                    wi = await db.get(WorkItem, session.work_item_id)
                    if wi is not None and wi.subtasks:
                        updated, changed = st.verify(wi.subtasks, tracked)
                        if changed:
                            wi.subtasks = updated
                            prog = st.progress(updated)
                            await _broadcast_event(
                                session_id, turn_id, seq, EventKind.log,
                                message=(
                                    f"sub-tasks: {prog['verified']} verified / "
                                    f"{prog['total']} confirmed"
                                ),
                                phase="subtasks_progress",
                                data={"work_item_id": wi.id, "progress": prog},
                            )
                except Exception:
                    logger.exception("[session] turn %d subtask verify failed", turn_id)
            # Substrate evidence: a turn that changed files gets its diff indexed
            # in the append-only store. Best-effort — never breaks the turn.
            if version is not None and session.project_id and version.get("diff"):
                from app import evidence as evidence_store
                await evidence_store.capture_safe(
                    db,
                    owner_id=owner_id,
                    project_id=session.project_id,
                    work_item_id=session.work_item_id,
                    session_turn_id=turn_id,
                    kind="diff",
                    filename=f"turn_{seq}.diff",
                    text=version["diff"],
                    producer=chosen,
                )
            await db.commit()

    await _broadcast_turn(turn_id, session_id, seq, chosen,
                          "succeeded" if final_result is not None else "failed")


async def run_turn(
    session_id: str,
    message: str,
    *,
    provider: str | None = None,
    owner_id: str = "local",
) -> int:
    """
    Execute one turn synchronously. Returns the SessionTurn id.
    Used by tests and callers that need to await completion.
    The HTTP endpoint now uses create_turn_record + run_turn_background instead.
    """
    turn_id, _ = await create_turn_record(
        session_id, message, provider=provider, owner_id=owner_id,
    )
    await run_turn_background(turn_id, session_id, owner_id=owner_id)
    return turn_id


async def capture_terminal_snapshot(
    session_id: str,
    *,
    provider: str | None = None,
    owner_id: str = "local",
) -> int | None:
    """
    Capture the web-TTY terminal lane's output as artifacts (D-0017 thread 2).

    The human-driven CLI edits the workspace directly with no engine commit
    boundary, so we snapshot the workspace on demand (Capture button) or on
    session stop, then record it as a SessionTurn so the same artifact-card
    rendering surfaces the files the session produced — the "capture the
    artifacts" reframe applied to the terminal lane (dissolves the grok
    viewport-truncation limit: the deliverable is the files, not the screen).

    Returns the new turn id, or None if the workspace was unchanged (nothing to
    capture — no empty turn is created). Raises SessionError for unknown sessions.
    """
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None or session.owner_id != owner_id:
            raise SessionError(f"session {session_id} not found")
        workspace = session.workspace_path
        label = provider or session.provider or "terminal"

    version = await ws.commit_snapshot(
        workspace, message=f"terminal session ({label})"
    )
    if version is None:
        return None  # nothing changed — don't record an empty turn

    async with AsyncSessionLocal() as db:
        seq = await _next_turn_seq(db, session_id)
        turn = SessionTurn(
            session_id=session_id,
            owner_id=owner_id,
            seq=seq,
            provider=label,
            prompt="⌨ terminal session",
            response="",
            status="succeeded",
            commit_sha=version["commit"],
            diffstat=version["diffstat"],
            changed_files=json.dumps(version["files"]),
            finished_at=datetime.now(UTC),
        )
        db.add(turn)
        session = await db.get(Session, session_id)
        if session is not None:
            session.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(turn)
        turn_id = turn.id

    # Structured ledger entry for the terminal session (D-0017 thread 1).
    ws.record_turn(
        workspace, seq=seq, provider=label,
        summary="terminal session", files=version["files"], lane="terminal",
    )

    await _broadcast_turn(turn_id, session_id, seq, label, "succeeded")
    await _broadcast_event(
        session_id, turn_id, seq, EventKind.result,
        message=f"version {version['short']} captured",
        phase="version",
        data={"commit": version["commit"], "short": version["short"],
              "diffstat": version["diffstat"], "diff": version["diff"],
              "files": version["files"]},
    )
    return turn_id


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _next_turn_seq(db, session_id: str) -> int:
    result = await db.execute(
        select(SessionTurn)
        .where(SessionTurn.session_id == session_id)
        .order_by(SessionTurn.seq.desc())
        .limit(1)
    )
    last = result.scalar_one_or_none()
    return (last.seq + 1) if last else 0


async def _broadcast_turn(
    turn_id: int, session_id: str, seq: int, provider: str, status: str,
    *, extra: dict | None = None,
) -> None:
    try:
        await ws_manager.broadcast({
            "type": "session.turn.update",
            "session_id": session_id,
            "turn": {"id": turn_id, "seq": seq, "provider": provider, "status": status,
                     **(extra or {})},
        })
    except Exception as exc:  # pragma: no cover
        logger.debug("WS session turn broadcast error: %s", exc)


async def _broadcast_event(
    session_id: str, turn_id: int, seq: int, kind: EventKind,
    *, message: str = "", text: str | None = None, phase: str = "",
    data: dict | None = None,
) -> None:
    try:
        safe_data = {k: v for k, v in (data or {}).items() if not isinstance(v, ExecResult)}
        await ws_manager.broadcast({
            "type": "session.event",
            "session_id": session_id,
            "turn_id": turn_id,
            "turn_seq": seq,
            "event": {"kind": kind.value, "message": message, "text": text,
                      "phase": phase, "data": safe_data},
        })
    except Exception as exc:  # pragma: no cover
        logger.debug("WS session event broadcast error: %s", exc)
