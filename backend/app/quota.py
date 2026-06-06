"""
quota.py — Provider health tracking: cooldown-on-rate-limit + best-effort ledger (§4.4).

This is best-effort, NOT a precise fuel gauge. The reliable guarantee is
failover on *observed* rate-limits, not prediction before them.

Health shape per provider:
  { healthy, cooldown_until, last_reset_seen, est_used_pct }

The README and UI must label any headroom estimate as approximate.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_SECONDS = 300  # 5-min fallback when no reset timestamp parsed


@dataclass
class ProviderHealth:
    healthy: bool = True
    cooldown_until: Optional[datetime] = None
    last_reset_seen: Optional[datetime] = None
    # Best-effort estimates (approximate — clearly labelled in UI)
    est_invocations: int = 0
    est_tokens: int = 0
    # Operator-declared limit window (optional; used for headroom estimate)
    declared_window_seconds: Optional[int] = None
    declared_window_limit: Optional[int] = None
    # Subscription quota scraped from the plan-CLI's /usage panel (D-0015 slice 4).
    # A real reading, so it takes precedence over the invocation-count estimate.
    subscription_used_pct: Optional[float] = None
    subscription_seen_at: Optional[datetime] = None

    @property
    def est_used_pct(self) -> Optional[float]:
        if self.subscription_used_pct is not None:
            return min(1.0, max(0.0, self.subscription_used_pct))
        if self.declared_window_limit and self.declared_window_limit > 0:
            return min(1.0, self.est_invocations / self.declared_window_limit)
        return None


class QuotaTracker:
    """
    Thread-safe provider health + best-effort usage ledger.

    Singleton per process; the orchestrator and router both reference it.
    """

    def __init__(self) -> None:
        self._health: dict[str, ProviderHealth] = defaultdict(ProviderHealth)
        self._lock = threading.Lock()

    def _check_expiry(self, provider: str) -> bool:
        """Check if cooldown expired; reset if so. Caller MUST hold _lock."""
        h = self._health[provider]
        if h.cooldown_until is not None:
            now = datetime.now(timezone.utc)
            if now >= h.cooldown_until:
                logger.info("[quota] %s cooldown expired, marking healthy", provider)
                h.healthy = True
                h.last_reset_seen = now
                h.cooldown_until = None
        return h.healthy

    def is_healthy(self, provider: str) -> bool:
        """Return True if the provider is not in cooldown."""
        with self._lock:
            return self._check_expiry(provider)

    def mark_cooldown(self, provider: str, reset_at: Optional[datetime] = None) -> None:
        """
        Called when a provider returns a rate-limit signal.
        Sets cooldown_until = reset_at (or now + default if not parsed).
        """
        import traceback
        with self._lock:
            h = self._health[provider]
            h.healthy = False
            if reset_at is not None:
                h.cooldown_until = reset_at
            else:
                h.cooldown_until = datetime.now(timezone.utc) + timedelta(
                    seconds=_DEFAULT_COOLDOWN_SECONDS
                )
            logger.warning(
                "[quota] %s marked in cooldown until %s\nCaller:\n%s",
                provider, h.cooldown_until,
                "".join(traceback.format_stack(limit=8)),
            )

    def mark_healthy(self, provider: str) -> None:
        with self._lock:
            h = self._health[provider]
            h.healthy = True
            h.cooldown_until = None

    def record_invocation(self, provider: str, tokens: int = 0) -> None:
        """Best-effort usage accounting."""
        with self._lock:
            h = self._health[provider]
            h.est_invocations += 1
            h.est_tokens += tokens

    def set_declared_limits(
        self, provider: str, *, window_seconds: int, window_limit: int
    ) -> None:
        """Operator-declared limit window for headroom estimate."""
        with self._lock:
            h = self._health[provider]
            h.declared_window_seconds = window_seconds
            h.declared_window_limit = window_limit

    def set_subscription_usage(
        self, provider: str, *, used_pct: Optional[float], reset_at: Optional[datetime] = None
    ) -> None:
        """Record a subscription-quota reading scraped from a plan-CLI /usage panel.

        used_pct is a 0..1 fraction; None clears it. When a reset_at is parsed it
        also seeds the cooldown so the router knows when the plan refills.
        """
        with self._lock:
            h = self._health[provider]
            h.subscription_used_pct = used_pct
            h.subscription_seen_at = datetime.now(timezone.utc)
            if reset_at is not None and used_pct is not None and used_pct >= 1.0:
                h.healthy = False
                h.cooldown_until = reset_at

    def get_health(self, provider: str) -> ProviderHealth:
        with self._lock:
            self._check_expiry(provider)
            return self._health[provider]

    def get_all_health(self) -> dict[str, ProviderHealth]:
        with self._lock:
            return dict(self._health)

    def earliest_reset(self, providers: list[str]) -> Optional[datetime]:
        """Return the earliest cooldown_until across a list of providers."""
        times = []
        with self._lock:
            for p in providers:
                h = self._health[p]
                if h.cooldown_until:
                    times.append(h.cooldown_until)
        return min(times) if times else None


# Module-level singleton
quota_tracker = QuotaTracker()
