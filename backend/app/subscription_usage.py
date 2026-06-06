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
from app.providers.registry import get_executor
from app.quota import quota_tracker

logger = logging.getLogger(__name__)

# "45% used", "Used 45%", "45 %", possibly with a bar before it.
_PCT_RE = re.compile(r"(\d{1,3})\s*%")
# "Resets <when>" / "resets at <when>" / "resets in <when>"
_RESET_RE = re.compile(r"resets?\s+(?:at|in|on|after)?\s*[:]?\s*(.+?)(?:[.\n]|$)", re.IGNORECASE)


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

    Tolerant by design — panels vary per CLI and version. We take the *highest*
    percentage shown (the binding limit) and the first reset hint. No percentage
    found ⇒ ok=False but the raw text is preserved for inspection.
    """
    out = SubscriptionUsage(instance_id=instance_id, raw=(text or "").strip())
    if not text:
        out.error = "empty panel"
        return out

    pcts = [int(m) for m in _PCT_RE.findall(text) if 0 <= int(m) <= 100]
    if pcts:
        out.used_pct = max(pcts) / 100.0
        out.ok = True
    else:
        out.error = "no percentage found in panel"

    m = _RESET_RE.search(text)
    if m:
        hint = m.group(1).strip()
        # Guard against swallowing a whole paragraph.
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

    scraped: list[str] = []
    err: Optional[str] = None
    try:
        async for ev in executor.run_stream(
            "",  # no task prompt — we only want the /usage panel
            workdir="/tmp",
            tools_enabled=False,
            extra={"control_commands": ["/usage"], "idle_timeout": timeout_hint},
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
