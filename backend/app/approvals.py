"""
app/approvals.py — pending human-approval registry (P-0046 code-exec confirmation).

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
"""
from __future__ import annotations

import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)

# request_id -> Future[bool] (True = approved)
_PENDING: dict[str, asyncio.Future[bool]] = {}

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
    return True


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
