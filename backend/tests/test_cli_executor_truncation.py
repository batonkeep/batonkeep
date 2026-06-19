"""
tests/test_cli_executor_truncation.py — grok turn-cap / cancellation handling.

A grok run that exhausts its turns emits a run of `{"type":"thought",...}` events,
then `{"type":"max_turns_reached"}`, then a terminal `{"type":"end",
"stopReason":"Cancelled"}` — without ever producing the deliverable. The executor
must report an HONEST FAILURE (an error event), not a falsely-"complete"
plain-text fallback built from partial reasoning text.

The subprocess is faked; no live grok binary is needed.
"""
from __future__ import annotations

import pytest

from app import sandbox
from app.providers.base import EventKind
from app.providers.cli_executor import CLIExecutor
from app.providers.registry import ProviderDef


class _FakeStream:
    """Async-iterable stand-in for proc.stdout / proc.stderr."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def _gen():
            for ln in self._lines:
                yield ln
        return _gen()


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes], stderr_lines: list[bytes]) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.returncode = 0

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:  # pragma: no cover - only hit on timeout
        self.returncode = -9


def _grok_def() -> ProviderDef:
    return ProviderDef(name="grok", kind="cli", tier="agent", cli_binary="grok")


async def _collect(executor: CLIExecutor) -> list:
    return [ev async for ev in executor.run_stream("research task", workdir="/tmp")]


@pytest.mark.asyncio
async def test_grok_max_turns_cancel_reports_failure(monkeypatch):
    # Sandbox is a no-op in tests (spawner absent, not required).
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: False)

    stdout_lines = [
        b'{"type":"thought","data":"Let me search"}\n',
        b'{"type":"thought","data":" the Google blog."}\n',
        b'{"type":"max_turns_reached"}\n',
        b'{"type":"end","stopReason":"Cancelled"}\n',
    ]

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(stdout_lines, [])

    monkeypatch.setattr(
        "app.providers.cli_executor.asyncio.create_subprocess_exec", _fake_exec
    )

    events = await _collect(CLIExecutor(_grok_def()))

    # No result event must be emitted — the run produced no deliverable.
    assert not any(ev.kind == EventKind.result for ev in events)
    errors = [ev for ev in events if ev.kind == EventKind.error]
    assert errors, "truncated run must surface an error event"
    assert "did not complete" in errors[-1].message
    assert "max turns reached" in errors[-1].message


@pytest.mark.asyncio
async def test_grok_normal_completion_still_synthesizes_result(monkeypatch):
    """A normal EndTurn run with delta text still produces a plain-text fallback
    result — the truncation guard must not regress the happy path."""
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: False)

    stdout_lines = [
        b'{"type":"text","data":"## Report\\n"}\n',
        b'{"type":"text","data":"All good."}\n',
        b'{"type":"end","stopReason":"EndTurn"}\n',
    ]

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(stdout_lines, [])

    monkeypatch.setattr(
        "app.providers.cli_executor.asyncio.create_subprocess_exec", _fake_exec
    )

    events = await _collect(CLIExecutor(_grok_def()))

    results = [ev for ev in events if ev.kind == EventKind.result]
    assert results, "normal completion must still synthesize a result"
    assert "All good." in results[-1].data["result"].text
    assert not any(ev.kind == EventKind.error for ev in events)
