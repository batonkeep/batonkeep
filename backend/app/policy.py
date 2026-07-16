"""
policy.py — the single effective-policy resolution seam (D-0058 seam 1).

Execution paths resolve their declared run/turn constraints through
`resolve_effective_policy()` instead of reading Task/Session/Settings fields ad
hoc. Today this is a thin composition of the existing per-object fields with
deliberately unchanged semantics. The point is the seam: the Phase C PolicySet
inheritance chain (deployment → owner → project → work-item → run/turn,
narrow-only — durable-work-substrate plan §5.8) lands as an implementation
change inside `resolve_effective_policy()`, not as a rewrite of every call
site. `project` / `work_item` are accepted now and unused until that phase.

Scope note: this resolves *declared* policy (caps, gates, flags). Runtime
headroom computations (remaining session budget, daily-cap composition) stay
with their orchestrators — they are state, not policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.config import get_settings

if TYPE_CHECKING:  # avoid import cycles; call sites pass ORM objects
    from app.models import Project, Session, Task, WorkItem

#: Task-lane code-exec / per-run defaults live on the Task row; the session
#: lane's per-turn budget default lives in sessions/orchestrator. Neither
#: default moves here — the resolver reports what is *declared*, with None
#: meaning "lane default applies".


@dataclass(frozen=True)
class EffectivePolicy:
    """Declared execution constraints for one run/turn.

    Phase C (PolicySet inheritance) extends this with provider/tool
    allowlists, approval roles, retention, and blast-radius fields — resolved
    here, consumed through the same call sites.
    """

    exec_policy: str          # code-exec gate: off | confirmation | allow-safe | auto
    confidential: bool        # sovereignty (P-0009 #1): local-only routing when True
    budget_cap_usd: float | None  # declared spend cap (None = lane default applies)
    timeout_seconds: int      # wall-clock bound for the run/turn drive


def resolve_effective_policy(
    *,
    task: Task | None = None,
    session: Session | None = None,
    project: Project | None = None,      # Phase C: project-level PolicySet
    work_item: WorkItem | None = None,   # Phase C: work-item narrowing
) -> EffectivePolicy:
    """Resolve the effective declared policy for a task run or session turn.

    Exactly one of `task` / `session` is expected; passing neither yields the
    deployment defaults (used by callers that gate before an object exists).
    """
    settings = get_settings()

    if task is not None:
        routing: dict[str, Any] = task.routing or {}
        return EffectivePolicy(
            exec_policy=task.exec_policy,
            confidential=bool(routing.get("confidential")),
            budget_cap_usd=None,  # task lane: per-run budget is a lane default today
            timeout_seconds=task.timeout_seconds or settings.run_timeout_seconds,
        )

    if session is not None:
        return EffectivePolicy(
            exec_policy=session.exec_policy,
            confidential=bool(session.confidential),
            budget_cap_usd=session.budget_usd,
            timeout_seconds=settings.run_timeout_seconds,
        )

    return EffectivePolicy(
        exec_policy="confirmation",
        confidential=False,
        budget_cap_usd=None,
        timeout_seconds=settings.run_timeout_seconds,
    )
