"""
app/approvals.py — pending human-approval registry (P-0046 code-exec confirmation)
+ the durable approval record.

The `confirmation` execution policy requires a human to approve each code-exec
run. Interactive build sessions have that human; the executor stays a pure event
generator (it does not block on I/O), so the approval round-trip is wired here
instead:

  1. the session's code-exec dispatch calls `request(...)`, which registers a
     Future and returns its `request_id`;
  2. the session broadcasts an `approval` event carrying that id + the proposed
     code to the frontend, then awaits the Future;
  3. the operator approves/denies via `POST /api/sessions/{id}/approvals/{rid}`,
     which calls `resolve(rid, approved)` to complete the Future.

Futures are per-process (the data plane is single-process today). A pending
approval times out (treated as denied) so a closed tab can't wedge a turn.

Durability (substrate approval baseline): the Future is only the in-process
*wakeup*; the `Approval` row is the *record*. Every request is persisted
(`record_request`), every decision stamped (`settle`), and a restart expires
stranded pending rows (`reap_pending`) — mirroring run/turn reaping — so the
audit trail survives the process. Canonical-write proposals reuse the same
rows without a Future: their decision arrives via the approvals API, not an
awaiting coroutine.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Approval

logger = logging.getLogger(__name__)

# request_id -> Future[bool] (True = approved)
_PENDING: dict[str, asyncio.Future[bool]] = {}
# ids an operator actually decided (vs timeout) — lets the durable record say
# who decided. Checked-and-discarded by was_resolved().
_RESOLVED_IDS: set[str] = set()

DEFAULT_TIMEOUT_S = 300.0


def request() -> tuple[str, asyncio.Future[bool]]:
    """Register a pending approval; returns its id and the Future to await."""
    request_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[bool] = loop.create_future()
    _PENDING[request_id] = fut
    return request_id, fut


def resolve(request_id: str, approved: bool) -> bool:
    """Complete a pending approval. Returns False if the id is unknown/already
    settled (so the endpoint can 404)."""
    fut = _PENDING.get(request_id)
    if fut is None or fut.done():
        return False
    fut.set_result(approved)
    _RESOLVED_IDS.add(request_id)
    return True


def was_resolved(request_id: str) -> bool:
    """True if an operator decided this request (vs a timeout). Consumes the flag."""
    if request_id in _RESOLVED_IDS:
        _RESOLVED_IDS.discard(request_id)
        return True
    return False


def cancel(request_id: str) -> None:
    """Drop a pending approval without resolving (cleanup)."""
    _PENDING.pop(request_id, None)


async def await_decision(
    request_id: str, fut: asyncio.Future[bool], *, timeout: float = DEFAULT_TIMEOUT_S
) -> bool:
    """Await an approval decision; on timeout treat as denied and clean up."""
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        logger.info("[approvals] request %s timed out — treated as denied", request_id)
        return False
    finally:
        _PENDING.pop(request_id, None)


# ── Durable rows ──────────────────────────────────────────────────────────────

async def record_request(
    db: AsyncSession,
    *,
    owner_id: str,
    request_id: str,
    kind: str,
    payload: dict | None = None,
    producer: str = "system",
    project_id: str | None = None,
    work_item_id: int | None = None,
    session_id: str | None = None,
    run_id: int | None = None,
) -> Approval:
    """Persist the durable record for a pending approval. Flushes; caller commits."""
    row = Approval(
        owner_id=owner_id,
        request_id=request_id,
        kind=kind,
        status="pending",
        payload=payload,
        producer=producer[:96],
        project_id=project_id,
        work_item_id=work_item_id,
        session_id=session_id,
        run_id=run_id,
    )
    db.add(row)
    await db.flush()
    return row


async def settle(
    db: AsyncSession, request_id: str, *, approved: bool, decided_by: str = "human"
) -> Approval | None:
    """Stamp the decision onto the durable row (pending → approved/denied).
    Returns None for unknown/already-settled ids. Flushes; caller commits."""
    result = await db.execute(select(Approval).where(Approval.request_id == request_id))
    row = result.scalar_one_or_none()
    if row is None or row.status != "pending":
        return None
    row.status = "approved" if approved else "denied"
    row.decided_by = decided_by[:96]
    row.decided_at = datetime.now(UTC)
    await db.flush()
    return row


async def reap_pending() -> int:
    """Expire approval rows stranded by a restart (their Futures are gone, so no
    decision can ever land). Mirrors run/turn reaping; called from lifespan.
    Canonical-write proposals are NOT reaped — they carry no Future and stay
    decidable through the approvals API across restarts."""
    from app.db import AsyncSessionLocal

    reaped = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Approval).where(
                Approval.status == "pending", Approval.kind != "canonical_write"
            )
        )
        now = datetime.now(UTC)
        for row in result.scalars().all():
            row.status = "expired"
            row.decided_at = now
            reaped += 1
        if reaped:
            await db.commit()
    if reaped:
        logger.warning("[approvals] expired %d orphaned pending approval(s) on startup", reaped)
    return reaped
