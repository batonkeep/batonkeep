"""
test_approvals.py — the pending-approval registry (P-0046 slice 3b).
"""
from __future__ import annotations

import asyncio

from app import approvals


async def test_request_resolve_roundtrip():
    rid, fut = approvals.request()
    assert isinstance(rid, str) and not fut.done()
    assert approvals.resolve(rid, True) is True
    assert await approvals.await_decision(rid, fut) is True


async def test_resolve_denied():
    rid, fut = approvals.request()
    approvals.resolve(rid, False)
    assert await approvals.await_decision(rid, fut) is False


async def test_resolve_unknown_id_returns_false():
    assert approvals.resolve("does-not-exist", True) is False


async def test_double_resolve_is_rejected():
    rid, fut = approvals.request()
    assert approvals.resolve(rid, True) is True
    # already settled
    assert approvals.resolve(rid, False) is False
    await approvals.await_decision(rid, fut)


async def test_timeout_is_denied_and_cleans_up():
    rid, fut = approvals.request()
    decided = await approvals.await_decision(rid, fut, timeout=0.05)
    assert decided is False
    # cleaned up — a late resolve finds nothing
    assert approvals.resolve(rid, True) is False


async def test_await_decision_pending_then_resolved():
    rid, fut = approvals.request()

    async def resolver():
        await asyncio.sleep(0.02)
        approvals.resolve(rid, True)

    asyncio.ensure_future(resolver())
    assert await approvals.await_decision(rid, fut, timeout=2.0) is True
