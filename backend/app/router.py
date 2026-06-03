"""
router.py — Capability/capacity router (§4.3).

Resolves a per-task routing policy into an ordered candidate list of
currently-healthy providers, respecting DEPLOYMENT_MODE and cooldown state.

Strategies:
  capability   — pick highest-preference candidate whose tags match and is healthy
  fixed        — always the first candidate
  round_robin  — rotate across healthy candidates (quota spreading)
  cost_optimized — like capability but prefer lowest cost-per-token among healthy

router.resolve() returns:
  CandidatePlan(candidates=[...], overflow_to=...) or
  DeferredResult(deferred_until=...) when all candidates are cooling down
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.config import get_settings
from app.providers.registry import (
    ProviderDef,
    get_instance,
    get_provider_def,
)
from app.quota import QuotaTracker

logger = logging.getLogger(__name__)
_settings = get_settings()

# Round-robin counters per task (keyed by task routing candidates tuple)
_rr_counters: dict[tuple, int] = {}


@dataclass
class CandidatePlan:
    """Router resolved successfully — ordered candidate list ready for failover loop."""
    candidates: list[str]
    overflow_to: Optional[str] = None


@dataclass
class DeferredResult:
    """All candidates are cooling down; the run should be deferred."""
    deferred_until: Optional[datetime]
    cooling_providers: list[str]


def resolve(
    routing: dict,
    quota: QuotaTracker,
    *,
    deployment_mode: Optional[str] = None,
) -> CandidatePlan | DeferredResult:
    """
    Resolve a routing policy dict into an ordered candidate plan.

    Args:
        routing: The task's routing JSON (§4.3). Defaults are applied for missing keys.
        quota: The quota tracker to check cooldown state.
        deployment_mode: Override for testing; defaults to settings value.

    Returns:
        CandidatePlan or DeferredResult.
    """
    strategy = routing.get("strategy", "capability")
    raw_candidates: list[str] = routing.get("candidates", _settings.candidates_list)
    cap_tags: list[str] = routing.get("capability_tags", [])
    overflow_to: Optional[str] = routing.get("overflow_to")
    max_attempts: int = routing.get("max_attempts", 3)
    failover: bool = routing.get("failover", True)

    mode = deployment_mode or _settings.deployment_mode.value
    plan_cli_allowed = mode != "managed"

    # 1. Filter candidates by availability and deployment mode.
    # Candidates are instance ids ("claude", "claude:work"); each resolves to an
    # instance + its template ProviderDef. Cost/tags come from the template; health
    # and ordering are tracked per-instance so same-provider accounts fail over
    # independently.
    available: list[tuple[str, ProviderDef]] = []
    for cand in raw_candidates:
        inst = get_instance(cand)
        if inst is None:
            logger.debug("[router] instance %s not available, skipping", cand)
            continue
        pdef = get_provider_def(inst.template)
        if pdef is None:
            continue
        if pdef.kind == "cli" and not plan_cli_allowed:
            logger.debug("[router] %s excluded (managed mode forbids plan-CLI)", cand)
            continue
        available.append((inst.id, pdef))

    if not available:
        logger.warning("[router] no available providers after filtering")
        return DeferredResult(deferred_until=None, cooling_providers=[])

    # 2. Apply strategy to produce an ordered list of instance ids
    if strategy == "fixed":
        ordered = [available[0][0]]

    elif strategy == "round_robin":
        healthy_ids = [iid for iid, _ in available if quota.is_healthy(iid)]
        if not healthy_ids:
            # All cooling — return deferred
            all_ids = [iid for iid, _ in available]
            return DeferredResult(
                deferred_until=quota.earliest_reset(all_ids),
                cooling_providers=all_ids,
            )
        key = tuple(iid for iid, _ in available)
        idx = _rr_counters.get(key, 0) % len(healthy_ids)
        _rr_counters[key] = idx + 1
        # Start from idx, wrap around
        ordered = healthy_ids[idx:] + healthy_ids[:idx]

    elif strategy == "cost_optimized":
        # Sort healthy candidates by cost (cheapest first)
        def _cost(item: tuple[str, ProviderDef]) -> float:
            return item[1].cost_in_per_mtok + item[1].cost_out_per_mtok

        healthy = [item for item in available if quota.is_healthy(item[0])]
        if not healthy:
            all_ids = [iid for iid, _ in available]
            return DeferredResult(
                deferred_until=quota.earliest_reset(all_ids),
                cooling_providers=all_ids,
            )
        healthy.sort(key=_cost)
        ordered = [iid for iid, _ in healthy]

    else:
        # capability (default): ordered preference, filter by capability_tags + health
        ordered, cooling_providers = _resolve_capability(available, cap_tags, quota)

    # 3. If ordered is empty, all tag-matching candidates are cooling
    if not ordered:
        # Use the cooling list from _resolve_capability for capability strategy,
        # otherwise fall back to all available ids.
        if strategy == "capability":
            c_names = cooling_providers  # type: ignore[possibly-undefined]
        else:
            c_names = [iid for iid, _ in available]
        return DeferredResult(
            deferred_until=quota.earliest_reset(c_names),
            cooling_providers=c_names,
        )

    # 4. Cap at max_attempts
    if not failover:
        ordered = ordered[:1]
    else:
        ordered = ordered[:max_attempts]

    return CandidatePlan(candidates=ordered, overflow_to=overflow_to)


def _resolve_capability(
    providers: list[tuple[str, ProviderDef]],
    required_tags: list[str],
    quota: QuotaTracker,
) -> tuple[list[str], list[str]]:
    """
    From the ordered (instance_id, template) list:
    1. Keep only those whose capability_tags intersect required_tags (or required_tags is empty).
    2. Remove instances in cooldown.
    3. Return (matched_and_healthy_ids, cooling_among_matched_ids).

    Returns a tuple so callers can distinguish tag-mismatch from actual cooldown.
    """
    matched_healthy = []
    cooling = []
    for iid, pdef in providers:
        # Tag match: if no required tags, any provider qualifies
        if required_tags:
            if not set(required_tags).intersection(pdef.capability_tags):
                logger.debug(
                    "[router] %s skipped — tags %s don't match required %s",
                    iid, pdef.capability_tags, required_tags,
                )
                continue
        # Health check (tag matched — now check cooldown), per-instance
        if not quota.is_healthy(iid):
            logger.info("[router] %s skipped — in cooldown", iid)
            cooling.append(iid)
            continue
        matched_healthy.append(iid)
    return matched_healthy, cooling
