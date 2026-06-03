"""
providers/base.py — Executor ABC + shared event/result types (§7).

The orchestrator only ever talks to this interface — it doesn't care whether
the work happened on an open model, a frontier model, or a CLI agent.

EventKind: log | phase | token | tool | subagent | result | error | route
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional


class EventKind(str, Enum):
    log = "log"
    phase = "phase"
    token = "token"
    tool = "tool"
    subagent = "subagent"
    result = "result"
    error = "error"
    route = "route"


@dataclass
class ExecEvent:
    kind: EventKind
    message: str = ""
    phase: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    # For token events: the incremental text chunk
    text: Optional[str] = None

    def is_terminal(self) -> bool:
        return self.kind in (EventKind.result, EventKind.error)


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass
class ExecResult:
    text: str
    usage: Usage
    provider: str
    model: str


class Executor(ABC):
    """
    Abstract base for all provider backends.

    Implementations: ModelExecutor, CLIExecutor, MockExecutor.
    """

    name: str
    tier: str  # open | frontier | agent | mock

    @property
    def kind(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def run_stream(
        self,
        prompt: str,
        *,
        workdir: str,
        tools_enabled: bool = True,
        max_rounds: int = 10,
        budget_usd: float = 1.0,
        extra: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[ExecEvent]:
        """
        Yield ExecEvents. The terminal event (kind=result or kind=error)
        carries an ExecResult in data["result"] or the error message.

        Must never block the event loop.
        """
        ...  # pragma: no cover

    @abstractmethod
    def is_healthy(self) -> bool:
        """Return True if this executor can accept a run right now."""
        ...
