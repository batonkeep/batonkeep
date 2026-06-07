"""
sessions/ledger.py — optional LLM summarizer for the session ledger (D-0017
thread 1, slice 2).

The deterministic ## Activity log (workspace.record_turn) is always on. This
module maintains the rolling ## Summary above it with a cheap model so a
switched-in provider is richly primed — the cross-provider memory bet (P-0020
thread 1). Cadence: on provider-switch + on-demand (founder, 2026-06-07).

Sovereignty (P-0009 #1, carried into D-0017): a **confidential** session never
sends its ledger to a remote model — it summarizes on an available local model
or skips entirely (the deterministic ledger stands). Non-confidential sessions
prefer an explicit `ledger_summary_provider`, else the session's current model.

Locality (D-0017): the summary is written into the user-owned workspace
SESSION.md, never to Batonkeep central. Best-effort throughout — a summarizer
failure must never break a turn.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Session
from app.providers.base import EventKind, ExecResult
from app.providers.registry import (
    get_executor,
    is_local_instance,
    local_candidate_ids,
)
from app.sessions import workspace as ws

logger = logging.getLogger(__name__)
_settings = get_settings()

_SUMMARY_PROMPT = """\
You are maintaining a concise, durable memory for a build session so a different \
AI agent can be handed the work and continue seamlessly. Below is the session \
ledger (goal, prior summary, and an activity log of what each turn changed).

Write an updated summary in at most {max_chars} characters: the goal, the current \
state of the work, key decisions made, and what is left to do. Be specific and \
factual — reference the actual files and changes. Do NOT invent progress that the \
ledger does not show. Output only the summary text (no preamble, no headings).

--- LEDGER ---
{brief}
--- END LEDGER ---
"""


def _pick_summarizer(current_provider: Optional[str], confidential: bool) -> Optional[str]:
    """Choose the instance id to summarize with, honouring the sovereignty rule.

    Confidential → an available local instance only (or None to skip). Otherwise
    prefer the configured summarizer, then the session's current provider, then any
    available instance. Availability is gated by get_executor (None if unusable)."""
    if confidential:
        for cid in local_candidate_ids():
            if get_executor(cid) is not None:
                return cid
        return None

    candidates = []
    if _settings.ledger_summary_provider:
        candidates.append(_settings.ledger_summary_provider)
    if current_provider:
        candidates.append(current_provider)
    # Fall back to any available local instance (cheap + always zero-cost).
    candidates.extend(local_candidate_ids())
    for cid in candidates:
        if cid and get_executor(cid) is not None:
            return cid
    return None


async def _run_single_shot(executor, prompt: str, workspace: str) -> str:
    """One model turn, no tools, tight budget — returns the result text or ''."""
    final: Optional[ExecResult] = None
    try:
        async for ev in executor.run_stream(
            prompt,
            workdir=workspace,
            tools_enabled=False,
            max_rounds=1,
            budget_usd=0.05,
            extra={"summary": True},
        ):
            if ev.kind == EventKind.result:
                final = ev.data.get("result")
            elif ev.kind == EventKind.error:
                logger.info("ledger summary executor error: %s", ev.message)
                break
    except Exception as exc:  # noqa: BLE001 — summarization is best-effort
        logger.warning("ledger summary run failed: %s", exc)
        return ""
    return (final.text if final else "").strip()


async def summarize_session(
    session_id: str, *, owner_id: str = "local", force: bool = False
) -> Optional[str]:
    """
    Refresh the session's ## Summary with a model-written digest of the ledger.

    Returns the new summary text, or None if it was skipped (disabled and not
    forced, unknown session, no eligible model, or an empty result). `force=True`
    (on-demand) bypasses the enabled flag but still honours the sovereignty rule.
    Never raises.
    """
    if not (_settings.ledger_summary_enabled or force):
        return None

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None or session.owner_id != owner_id:
            return None
        workspace = session.workspace_path
        confidential = session.confidential
        current_provider = session.provider

    chosen = _pick_summarizer(current_provider, confidential)
    if chosen is None:
        logger.info(
            "ledger summary skipped for %s: no eligible model (confidential=%s)",
            session_id, confidential,
        )
        return None
    # Defence in depth: never let a confidential session resolve to a remote model.
    if confidential and not is_local_instance(chosen):
        return None

    executor = get_executor(chosen)
    if executor is None:
        return None

    brief = ws.read_brief(workspace)
    if not brief.strip():
        return None
    prompt = _SUMMARY_PROMPT.format(brief=brief, max_chars=_settings.ledger_summary_max_chars)

    text = await _run_single_shot(executor, prompt, workspace)
    if not text:
        return None
    text = text[: _settings.ledger_summary_max_chars]
    ws.set_summary(workspace, text)
    logger.info("ledger summary refreshed for %s via %s", session_id, chosen)
    return text
