"""app/attribution.py — the attribution envelope (D-0059 D3 / P-0103).

Every material event records **who**. D3 states the guarantee precisely: *identity is
externally issued; attribution is the OSS guarantee*, and every material event
distinguishes the three actors that can differ —

* **`initiated_by`** — who *wanted* it. A human asking, or an agent delegating.
* **`executed_by`** — who *did* it. The lane/provider that actually ran.
* **`delegated_by`** — set only when the initiator was itself acting for someone else,
  i.e. the agent-to-agent case. NULL is the common, honest answer.

`principal_id`/`principal_kind` is the envelope's subject: the actor the event is *about*.

**Why this lands before agents can talk to each other.** [[D-0059]] marks these fields
**can't-retrofit** on approvals, evidence, memory and canonical commits — not because
adding a column is hard, but because *rows accumulate*. Every approval written without an
envelope is an adjudication whose actor can never be recovered, and the adjudication
record is the thing [[R8]] says is our uncontested residue. Once agent A can ask agent B
to do something, "which agent proposed this" must already have had somewhere to go.

**Why a scheme, not a bare string.** `producer` already exists and is free-form — it holds
`"human"`, `"system"`, or a provider instance id interchangeably, so it cannot be queried
or trusted. Principals are typed and namespaced instead, so a reader can tell a person
from a lane from a scheduler without parsing prose, and so a future durable agent slots
into the reserved namespace without touching a single existing row.

**Local mode is one implicit operator** (D3). We are not growing accounts; `human:local`
is the whole human namespace until the `AUTH_MODE` verify seam builds at its Phase-F
trigger.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Principal namespaces. Typed on purpose — see the module docstring.
KIND_HUMAN = "human"
KIND_AGENT = "agent"
KIND_SYSTEM = "system"

PRINCIPAL_KINDS = frozenset({KIND_HUMAN, KIND_AGENT, KIND_SYSTEM})


def human(owner_id: str = "local") -> str:
    """The operator. One implicit principal in local mode (D3)."""
    return f"{KIND_HUMAN}:{owner_id}"


def system(component: str) -> str:
    """The engine acting on its own — the scheduler firing, a reaper reconciling.

    Distinct from an agent: nothing *decided* anything, so attributing it to an agent
    would overstate what happened, and attributing it to the operator would be false.
    """
    return f"{KIND_SYSTEM}:{component}"


def agent(lane: str, ref: str | None = None) -> str:
    """A non-human actor that produced work.

    Until a durable Agent entity exists ([[P-0107]] shape (c)), an agent is identified by
    the **lane plus the durable thing it was acting on** — `agent:planner/project-abc`,
    `agent:run/task-12`. That is an honest identity for what exists today: it is stable
    across restarts and it names something a reader can go and look at.

    It is deliberately *not* the provider instance id. The provider is *which model
    answered*, which can change mid-task by design (D-0008 cross-provider switching) —
    attributing an adjudication to `claude-api` would record the backend, not the actor,
    and would make the same agent look like several.
    """
    return f"{KIND_AGENT}:{lane}" + (f"/{ref}" if ref else "")


@dataclass(frozen=True)
class Envelope:
    """Who a material event is about, and who set it in motion.

    Kept as a small value object rather than loose kwargs so that adding the fourth actor
    D3 anticipates (`delegated_by`, once agents delegate) is a change in one place, and so
    a caller cannot silently pass two of the three and look complete.
    """

    principal_id: str
    principal_kind: str
    initiated_by: str | None = None
    executed_by: str | None = None
    delegated_by: str | None = None

    def as_columns(self) -> dict[str, str | None]:
        return {
            "principal_id": self.principal_id,
            "principal_kind": self.principal_kind,
            "initiated_by": self.initiated_by,
            "executed_by": self.executed_by,
            "delegated_by": self.delegated_by,
        }


def by_human(owner_id: str = "local", *, executed_by: str | None = None) -> Envelope:
    """The operator did it themselves, or asked for it directly."""
    who = human(owner_id)
    return Envelope(
        principal_id=who, principal_kind=KIND_HUMAN,
        initiated_by=who, executed_by=executed_by or who,
    )


def by_agent(lane: str, ref: str | None = None, *, initiated_by: str | None = None,
             delegated_by: str | None = None) -> Envelope:
    """An agent produced it.

    `initiated_by` defaults to the operator, because today every agent action traces back
    to a human who scheduled or started it. `delegated_by` stays NULL until agents can
    ask each other — at which point it is the field that says so, and it already exists.
    """
    who = agent(lane, ref)
    return Envelope(
        principal_id=who, principal_kind=KIND_AGENT,
        initiated_by=initiated_by or human(), executed_by=who,
        delegated_by=delegated_by,
    )


def by_system(component: str) -> Envelope:
    """The engine acted on its own — scheduler, reaper, sweep."""
    who = system(component)
    return Envelope(
        principal_id=who, principal_kind=KIND_SYSTEM,
        initiated_by=who, executed_by=who,
    )
