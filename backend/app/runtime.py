"""Process identity for boot-time reconciliation (D-0068).

`RUNTIME_EPOCH` is generated once per backend process. It is stamped onto a run when the
orchestrator claims it, so a startup reconciliation can ask *"did **this** process start
this run?"* instead of assuming that anything non-terminal at boot must be orphaned.

That assumption is true of a single process and false of two, and it is exactly what would
turn the durable queue of P-0099 into a double-execution bug the first time the control
plane runs more than one replica (ARCHITECTURE §6). Keeping the epoch cheap and in-process
is deliberate: it needs no coordination, no clock sync and no extra dependency, and it is
correct for the single-process case we ship today while staying correct when that changes.
"""
from __future__ import annotations

from uuid import uuid4

#: Regenerated on every import of a fresh process — never persisted across restarts.
RUNTIME_EPOCH: str = uuid4().hex
