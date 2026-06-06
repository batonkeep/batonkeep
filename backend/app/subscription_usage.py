"""
subscription_usage.py — Plan-CLI /usage capture via the terminal seam (D-0015 #4).

Closes the half of P-0009 #2 that the headless seam couldn't reach: subscription
plans are zero *marginal* cost but still have a quota (weekly/5-hour limits), and
that quota only shows up in the interactive `/usage` panel. This module drives a
CLI's `/usage` through the PTY terminal seam, scrapes the panel text, parses it
into a structured reading, and feeds the percentage into the quota tracker so it
surfaces on ProviderHealth.est_used_pct (and thus the /api/usage cost surface).

The capture is on-demand and console-gated (it spawns a CLI); parsing is the pure,
testable core. `/usage` must be on the terminal allow-policy for the seam to send
it, and the instance's exec seam must be "terminal".

API:
    parse_usage_panel(text) -> SubscriptionUsage
    capture_subscription_usage(instance_id) -> SubscriptionUsage
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.providers.base import EventKind
from app.providers.registry import get_executor, get_instance, get_provider_def
from app.quota import quota_tracker

logger = logging.getLogger(__name__)

# The control command that opens each plan-CLI's usage/quota panel differs per CLI
# (verified live 2026-06-06). The first token must be on the terminal allow-policy.
# Capture status (2026-06-06): claude/codex/agy parse reliably; grok is best-effort
# — its credit panel is redraw-heavy and loads async, so a naive scrape often misses
# it (a virtual-terminal screen buffer would fix it; tracked as follow-up).
_USAGE_COMMAND = {
    "claude": "/usage",        # "NN% used"
    "codex": "/status",        # "NN% left"  → used = 100-NN
    "agy": "/usage",           # "NN% available" per model → used = 100-NN
    "grok": "/usage show",     # credit-based panel; best-effort
}
_DEFAULT_USAGE_COMMAND = "/usage"

# A percentage anywhere in the panel: "45% used", "Used 45%", "45 %".
_PCT_RE = re.compile(r"(\d{1,3})\s*%")
# Words that flip a percentage's meaning. CLIs disagree on framing:
#   claude → "95% used"            (used)
#   codex  → "87% left"            (remaining → used = 100-87)
#   agy    → "100% Quota available"(remaining → used = 100-100 = 0)
_USED_WORDS = ("used", "consumed", "spent")
_REMAIN_WORDS = ("left", "remain", "remaining", "available", "free")
# How many chars around a "%" we scan for a framing word.
_CTX_WINDOW = 30
# "Resets <when>" / "resets at <when>" / "resets in <when>"
_RESET_RE = re.compile(r"resets?\s+(?:at|in|on|after)?\s*[:]?\s*(.+?)(?:[.\n]|$)", re.IGNORECASE)
# Box-drawing / bar glyphs to trim from a scraped reset hint.
_HINT_TRIM = "│|╮╯╭╰─━█▌▔ \t"


@dataclass
class SubscriptionUsage:
    instance_id: str
    used_pct: Optional[float] = None      # 0..1, the highest limit-bar found
    reset_hint: Optional[str] = None      # raw human reset string, if any
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: str = ""                          # scraped panel text (trimmed)
    ok: bool = False                       # whether we got a usable reading
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "used_pct": self.used_pct,
            "reset_hint": self.reset_hint,
            "captured_at": self.captured_at.isoformat(),
            "ok": self.ok,
            "error": self.error,
            "raw": self.raw[:2000],
        }


def parse_usage_panel(text: str, instance_id: str = "") -> SubscriptionUsage:
    """Parse a scraped /usage panel into a SubscriptionUsage.

    Tolerant by design — panels vary per CLI and version. We only count a
    percentage that sits next to a framing word ("used" vs "left"/"available"),
    which (a) normalises the meaning to *used* across CLIs and (b) ignores stray
    numbers from menus/banners, killing false positives. The highest used% is the
    binding constraint. No framed percentage ⇒ ok=False, raw preserved.
    """
    out = SubscriptionUsage(instance_id=instance_id, raw=(text or "").strip())
    if not text:
        out.error = "empty panel"
        return out

    norm = text.replace("\r", "\n")
    used_values: list[int] = []
    for m in _PCT_RE.finditer(norm):
        pct = int(m.group(1))
        if not 0 <= pct <= 100:
            continue
        ctx = norm[max(0, m.start() - _CTX_WINDOW): m.end() + _CTX_WINDOW].lower()
        if any(w in ctx for w in _REMAIN_WORDS):
            used_values.append(100 - pct)
        elif any(w in ctx for w in _USED_WORDS):
            used_values.append(pct)
        # bare percentage with no framing word → ignored (likely not a quota gauge)

    if used_values:
        out.used_pct = max(used_values) / 100.0
        out.ok = True
    else:
        out.error = "no quota percentage found in panel"

    m = _RESET_RE.search(norm)
    if m:
        hint = m.group(1).strip().strip(_HINT_TRIM)
        out.reset_hint = hint[:120] or None

    return out


async def capture_subscription_usage(instance_id: str, *, timeout_hint: float = 20.0) -> SubscriptionUsage:
    """Drive `/usage` through the terminal seam for one instance and parse it.

    Returns a SubscriptionUsage; on a usable reading, pushes used_pct into the
    quota tracker so ProviderHealth/​/api/usage reflect the subscription quota.
    Errors (seam off, /usage not allowed, executor missing) come back as ok=False.
    """
    executor = get_executor(instance_id)
    if executor is None:
        return SubscriptionUsage(instance_id=instance_id, error="unknown or unavailable instance")

    # Pick the usage-panel command for this provider (differs per CLI).
    inst = get_instance(instance_id)
    pdef = get_provider_def(inst.template) if inst else None
    provider = pdef.name if pdef else instance_id
    command = _USAGE_COMMAND.get(provider, _DEFAULT_USAGE_COMMAND)

    scraped: list[str] = []
    err: Optional[str] = None
    try:
        async for ev in executor.run_stream(
            "",  # no task prompt — we only want the usage panel
            workdir="/tmp",
            tools_enabled=False,
            extra={"control_commands": [command], "idle_timeout": timeout_hint},
        ):
            if ev.kind == EventKind.token and ev.text:
                scraped.append(ev.text)
            elif ev.kind == EventKind.result and not scraped and ev.text:
                scraped.append(ev.text)
            elif ev.kind == EventKind.error:
                err = ev.message
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("[subscription_usage] capture failed for %s", instance_id)
        return SubscriptionUsage(instance_id=instance_id, error=str(exc))

    if err and not scraped:
        return SubscriptionUsage(instance_id=instance_id, error=err)

    usage = parse_usage_panel("".join(scraped), instance_id=instance_id)
    if usage.ok:
        quota_tracker.set_subscription_usage(instance_id, used_pct=usage.used_pct)
        logger.info("[subscription_usage] %s at %.0f%% used", instance_id, (usage.used_pct or 0) * 100)
    return usage
