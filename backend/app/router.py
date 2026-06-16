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
from dataclasses import dataclass, field
from datetime import datetime

from app.config import get_settings
from app.providers.registry import (
    ProviderDef,
    effective_capability_tags,
    get_instance,
    get_provider_def,
    is_provider_enabled,
    local_candidate_ids,
)
from app.quota import QuotaTracker

logger = logging.getLogger(__name__)
_settings = get_settings()

# Round-robin counters per task (keyed by task routing candidates tuple)
_rr_counters: dict[tuple, int] = {}

# Version of the routing *policy* in force (P-0053). Stamped on every RoutingTrace
# so accumulated decisions can be partitioned by policy when a scored/learned
# policy later replaces (or augments) this rule-based one.
POLICY_VERSION = "rule-v1"


@dataclass
class RoutingTrace:
    """A pure, DB-free record of what the router considered and why (P-0053).

    The router stays DB-free: it only *describes* the decision here; the
    orchestrator persists it as a RoutingDecision row. Content-free by
    construction — only candidate metadata + features, never prompt/output.
    """
    strategy: str
    policy_version: str = POLICY_VERSION
    confidential: bool = False
    degraded: bool = False
    deployment_mode: str | None = None
    deferred: bool = False
    deciding_reason: str = ""
    requested_candidates: list[str] = field(default_factory=list)
    # per-candidate features at decision time:
    #   {instance, kind, free, cost_per_mtok, tags, status}
    # status ∈ {"chosen", "healthy", "cooling", "excluded:<reason>"}
    evaluated: list[dict] = field(default_factory=list)
    chosen: str | None = None
    chosen_candidates: list[str] = field(default_factory=list)
    overflow_to: str | None = None

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "policy_version": self.policy_version,
            "confidential": self.confidential,
            "degraded": self.degraded,
            "deployment_mode": self.deployment_mode,
            "deferred": self.deferred,
            "deciding_reason": self.deciding_reason,
            "requested_candidates": self.requested_candidates,
            "evaluated": self.evaluated,
            "chosen": self.chosen,
            "chosen_candidates": self.chosen_candidates,
            "overflow_to": self.overflow_to,
        }


@dataclass
class CandidatePlan:
    """Router resolved successfully — ordered candidate list ready for failover loop."""
    candidates: list[str]
    overflow_to: str | None = None
    trace: RoutingTrace | None = None


@dataclass
class DeferredResult:
    """All candidates are cooling down; the run should be deferred."""
    deferred_until: datetime | None
    cooling_providers: list[str]
    trace: RoutingTrace | None = None


def resolve(
    routing: dict,
    quota: QuotaTracker,
    *,
    deployment_mode: str | None = None,
    degrade_to_free: bool = False,
) -> CandidatePlan | DeferredResult:
    """
    Resolve a routing policy dict into an ordered candidate plan.

    Args:
        routing: The task's routing JSON (§4.3). Defaults are applied for missing keys.
        quota: The quota tracker to check cooldown state.
        deployment_mode: Override for testing; defaults to settings value.
        degrade_to_free: Budget policy layer (P-0009 #2). When True (owner over the
            daily cap), restrict candidates to zero-marginal-cost providers
            (subscription plan-CLIs + local models) and forbid overflow. Defers if
            none are available — graceful degradation, never a silent over-spend.

    Returns:
        CandidatePlan or DeferredResult.
    """
    strategy = routing.get("strategy", "capability")
    raw_candidates: list[str] = list(routing.get("candidates", _settings.candidates_list))
    cap_tags: list[str] = routing.get("capability_tags", [])
    overflow_to: str | None = routing.get("overflow_to")
    max_attempts: int = routing.get("max_attempts", 3)
    failover: bool = routing.get("failover", True)
    confidential = routing.get("sensitivity") == "confidential"
    mode = deployment_mode or _settings.deployment_mode.value

    # P-0053: build a pure decision trace as we go; the orchestrator persists it.
    # The router itself does no DB I/O ("router stays DB-free").
    trace = RoutingTrace(
        strategy=strategy,
        confidential=confidential,
        degraded=degrade_to_free,
        deployment_mode=mode,
        requested_candidates=list(raw_candidates),
    )
    # `excluded`/`available` are populated by step 1; _eval_list reads them.
    excluded: list[dict] = []
    available: list[tuple[str, ProviderDef]] = []

    def _features(iid: str, pdef: ProviderDef) -> dict:
        return {
            "instance": iid,
            "kind": pdef.kind,
            "free": bool(pdef.kind == "cli" or pdef.local),
            "cost_per_mtok": round(pdef.cost_in_per_mtok + pdef.cost_out_per_mtok, 4),
            "tags": list(effective_capability_tags(pdef)),
        }

    def _eval_list(chosen: list[str]) -> list[dict]:
        """Per-candidate features at decision time: excluded rows + each available
        candidate annotated chosen / healthy / cooling (health from the same quota
        source the strategy used)."""
        rows = list(excluded)
        chosen_set = set(chosen)
        for iid, pdef in available:
            if iid in chosen_set:
                status = "chosen"
            elif not quota.is_healthy(iid):
                status = "cooling"
            else:
                status = "healthy"
            rows.append({**_features(iid, pdef), "status": status})
        return rows

    def _defer(reason: str, *, until: datetime | None = None,
               cooling: list[str] | None = None) -> DeferredResult:
        trace.deferred = True
        trace.deciding_reason = reason
        # nothing chosen — annotate whatever was evaluated (cooling/healthy/excluded)
        trace.evaluated = _eval_list([])
        return DeferredResult(deferred_until=until, cooling_providers=cooling or [], trace=trace)

    def _plan(ordered: list[str], *, reason: str) -> CandidatePlan:
        trace.deciding_reason = reason
        trace.overflow_to = overflow_to
        trace.evaluated = _eval_list(ordered)
        trace.chosen_candidates = list(ordered)
        trace.chosen = ordered[0] if ordered else None
        return CandidatePlan(candidates=ordered, overflow_to=overflow_to, trace=trace)

    # Sovereignty boundary (P-0009 #1): a confidential task may only ever run on a
    # local provider — never a remote API/CLI. This is a policy layer over the
    # normal router: it replaces the candidate set with the available local
    # providers (ignoring any remote candidates the task declared) and forbids
    # overflow off-box. If no local provider is healthy the task defers — it must
    # fail closed, never silently fall back to a remote model.
    if confidential:
        raw_candidates = local_candidate_ids()
        overflow_to = None
        logger.info("[router] confidential — local providers only: %s", raw_candidates)
        if not raw_candidates:
            logger.warning("[router] confidential task, no local provider available — deferring")
            return _defer("confidential: no local provider available")

    # Budget boundary (P-0009 #2): when the owner is over the daily cap, degrade to
    # zero-marginal-cost providers only — subscription plan-CLIs + local models —
    # and forbid overflow off the free set. Applied *after* the confidential layer
    # so sovereignty always wins; a confidential candidate set is already local
    # (hence free) so this only narrows further among free providers, never widens.
    if degrade_to_free:
        free = [c for c in raw_candidates if _is_free_candidate(c)]
        overflow_to = None
        logger.info("[router] over budget — degrading to free providers only: %s", free)
        if not free:
            logger.warning("[router] over budget, no free provider available — deferring")
            return _defer("over budget: no zero-cost provider available")
        raw_candidates = free

    plan_cli_allowed = mode != "managed"

    # 1. Filter candidates by availability and deployment mode.
    # Candidates are instance ids ("claude", "claude:work"); each resolves to an
    # instance + its template ProviderDef. Cost/tags come from the template; health
    # and ordering are tracked per-instance so same-provider accounts fail over
    # independently.
    for cand in raw_candidates:
        inst = get_instance(cand)
        if inst is None:
            logger.debug("[router] instance %s not available, skipping", cand)
            excluded.append({"instance": cand, "status": "excluded:not_available"})
            continue
        pdef = get_provider_def(inst.template)
        if pdef is None:
            excluded.append({"instance": cand, "status": "excluded:no_provider_def"})
            continue
        if pdef.kind == "cli" and not plan_cli_allowed:
            logger.debug("[router] %s excluded (managed mode forbids plan-CLI)", cand)
            excluded.append({"instance": inst.id, "status": "excluded:managed_forbids_cli"})
            continue
        if not is_provider_enabled(inst.id):
            logger.debug("[router] %s excluded (operator-disabled)", cand)
            excluded.append({"instance": inst.id, "status": "excluded:operator_disabled"})
            continue
        available.append((inst.id, pdef))

    if not available:
        logger.warning("[router] no available providers after filtering")
        return _defer("no available providers after filtering")

    # 2. Apply strategy to produce an ordered list of instance ids
    if strategy == "fixed":
        ordered = [available[0][0]]

    elif strategy == "round_robin":
        healthy_ids = [iid for iid, _ in available if quota.is_healthy(iid)]
        if not healthy_ids:
            all_ids = [iid for iid, _ in available]
            return _defer("all candidates cooling (round_robin)",
                          until=quota.earliest_reset(all_ids), cooling=all_ids)
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
            return _defer("all candidates cooling (cost_optimized)",
                          until=quota.earliest_reset(all_ids), cooling=all_ids)
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
        return _defer("all tag-matched candidates cooling",
                      until=quota.earliest_reset(c_names), cooling=c_names)

    # 4. Cap at max_attempts
    if not failover:
        ordered = ordered[:1]
    else:
        ordered = ordered[:max_attempts]

    return _plan(ordered, reason=f"{strategy}: {len(ordered)} candidate(s) ordered")


def _is_free_candidate(candidate_id: str) -> bool:
    """True iff a candidate instance resolves to a zero-marginal-cost provider
    (subscription plan-CLI or local model). Mirrors cost.is_free_provider but
    keyed by instance id so the router stays DB-free."""
    inst = get_instance(candidate_id)
    if inst is None:
        return False
    pdef = get_provider_def(inst.template)
    return bool(pdef and (pdef.kind == "cli" or pdef.local))


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
        # Tag match: if no required tags, any provider qualifies. Use the
        # operator's tag override when set (P-0044), else the template tags.
        prov_tags = effective_capability_tags(pdef)
        if required_tags:
            if not set(required_tags).intersection(prov_tags):
                logger.debug(
                    "[router] %s skipped — tags %s don't match required %s",
                    iid, prov_tags, required_tags,
                )
                continue
        # Health check (tag matched — now check cooldown), per-instance
        if not quota.is_healthy(iid):
            logger.info("[router] %s skipped — in cooldown", iid)
            cooling.append(iid)
            continue
        matched_healthy.append(iid)
    return matched_healthy, cooling
