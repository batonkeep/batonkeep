"""app/notify.py — outbound notification for work that has stopped to ask a human.

P-0100 / Gate B4. An approval queue only helps an operator who *looks* at it; an
unattended agent that parks at 03:00 needs to reach out.

**Why an outbound webhook and not Web Push.** Web Push has no direct-delivery path: a
self-hosted server must route every message through the browser vendor's push service
(Google FCM for Chrome, Mozilla autopush for Firefox), authenticated with VAPID. Payloads
are encrypted end-to-end, but the dependency itself is the problem — a product whose pitch
is *your plans, your keys, your machine* should not require a third party to be reachable
before its operator can be told that their agent is waiting. It also simply fails on an
isolated network.

A webhook inverts that: we pick no vendor. The operator points it at their own ntfy or
Gotify instance, a chat hook, or a shell script, and chooses their own trust boundary.

Deliberately **not** SSRF-fenced, unlike `web_fetch`. That fence exists because a *model*
chooses those URLs from untrusted content; this URL is configured by the operator, who owns
the box — and a LAN target is the *expected* case here (a self-hosted ntfy on the same
network). Fencing it would block the sovereign configuration and protect nobody.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()


async def approval_pending(
    *, request_id: str, run_id: int | None, label: str | None, producer: str
) -> None:
    """Best-effort: tell the operator something is waiting. Never raises.

    A notification failure must never change what the agent does — the durable record and
    the in-app queue are the source of truth, and a wedged run because a chat server was
    down would be a far worse bug than a missed ping.
    """
    url = (_settings.approval_webhook_url or "").strip()
    if not url:
        return
    payload = {
        "event": "approval.pending",
        "request_id": request_id,
        "run_id": run_id,
        "label": label or "Approve code execution?",
        "producer": producer,
        # No code payload: the proposal can be long, and a notification hop is the wrong
        # place for it. The queue shows what is actually being approved.
        "text": f"Batonkeep: run #{run_id} is waiting on your approval ({producer}).",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.warning("[notify] approval webhook failed: %s", exc)
