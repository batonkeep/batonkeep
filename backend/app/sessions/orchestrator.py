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

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Session, SessionTurn
from app.providers.base import EventKind, ExecResult, Usage
from app.providers.registry import get_executor
from app.sessions import workspace as ws
from app.sessions.preview import rewrite_workspace_file_links
from app.ws import ws_manager

logger = logging.getLogger(__name__)
_settings = get_settings()


class SessionError(Exception):
    """Raised for caller-facing problems (unknown session/provider)."""


async def run_turn(
    session_id: str,
    message: str,
    *,
    provider: Optional[str] = None,
    owner_id: str = "local",
) -> int:
    """
    Execute one turn. Returns the SessionTurn id. Streams events over WS live.
    Raises SessionError for unknown session/provider before any turn is created.
    """
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None or session.owner_id != owner_id:
            raise SessionError(f"session {session_id} not found")

        chosen = provider or session.provider
        if not chosen:
            raise SessionError("no provider selected for this session")

        executor = get_executor(chosen)
        if executor is None:
            raise SessionError(f"provider {chosen!r} is not available")

        switched = chosen != session.provider
        session.provider = chosen

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
        turn_id = turn.id
        workspace = session.workspace_path

    await _broadcast_turn(turn_id, session_id, seq, chosen, "running",
                          extra={"switched": switched})
    if switched:
        await _broadcast_event(session_id, turn_id, seq, EventKind.route,
                               message=f"switched to {chosen}; continuing from workspace + SESSION.md")

    # Build context from the workspace, not a replayed transcript (D-0008).
    prompt = ws.build_turn_context(workspace, message)

    final_result: Optional[ExecResult] = None
    error_msg: Optional[str] = None

    try:
        async for ev in executor.run_stream(
            prompt,
            workdir=workspace,
            tools_enabled=_settings.autonomous_tools,
            max_rounds=10,
            budget_usd=1.0,
            extra={"session": True, "turn_seq": seq, "user_message": message},
        ):
            # Rewrite agent file:// links to the raw-file route so the result
            # renders with clickable artifacts in the live view (P-0016 b).
            ev_text = ev.text
            if ev.kind == EventKind.result and ev_text:
                ev_text = rewrite_workspace_file_links(ev_text, session_id, workspace)
            await _broadcast_event(session_id, turn_id, seq, ev.kind,
                                   message=ev.message, text=ev_text, phase=ev.phase, data=ev.data)
            if ev.kind == EventKind.result:
                final_result = ev.data.get("result")
            elif ev.kind == EventKind.error:
                error_msg = ev.message or "executor error"
                break
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("[session] turn %d executor error", turn_id)
        error_msg = str(exc)

    response_text = final_result.text if final_result else ""
    if response_text:
        # Persist with file:// links rewritten so reloads also get clickable
        # artifacts, not just the live stream (P-0016 b).
        response_text = rewrite_workspace_file_links(response_text, session_id, workspace)
    version: Optional[dict] = None
    if final_result is not None:
        ws.append_progress(
            workspace,
            f"turn {seq} ({chosen}): {(response_text.splitlines() or [''])[0][:120]}",
        )
        # Engine owns the commit boundary (D-0008): commit the workspace as a
        # version once the turn's edits have landed. None if nothing changed.
        version = await ws.commit_turn(
            workspace, seq=seq, provider=chosen, summary=response_text
        )

    if version is not None:
        # Surface the per-turn diff in the live event view (M1.3 Verify gate).
        await _broadcast_event(
            session_id, turn_id, seq, EventKind.result,
            message=f"version {version['short']} committed",
            phase="version",
            data={"commit": version["commit"], "short": version["short"],
                  "diffstat": version["diffstat"], "diff": version["diff"]},
        )

    async with AsyncSessionLocal() as db:
        turn = await db.get(SessionTurn, turn_id)
        if turn is not None:
            turn.finished_at = datetime.now(timezone.utc)
            if final_result is not None:
                turn.status = "succeeded"
                turn.response = response_text
            else:
                turn.status = "failed"
                turn.error = error_msg or "no result produced"
            if version is not None:
                turn.commit_sha = version["commit"]
                turn.diffstat = version["diffstat"]
            await db.commit()
        session = await db.get(Session, session_id)
        if session is not None:
            session.updated_at = datetime.now(timezone.utc)
            await db.commit()

    await _broadcast_turn(turn_id, session_id, seq, chosen,
                          "succeeded" if final_result is not None else "failed")
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
    *, extra: Optional[dict] = None,
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
    *, message: str = "", text: Optional[str] = None, phase: str = "",
    data: Optional[dict] = None,
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
