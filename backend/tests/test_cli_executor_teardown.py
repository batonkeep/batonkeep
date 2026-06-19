"""
tests/test_cli_executor_teardown.py — interrupt teardown safety (P-0057/D-0051).

The CLI runs as the `sandbox` uid, so `batond` cannot signal it cross-user — a
bare ``proc.kill()`` raises ``EPERM``. During an interrupt that EPERM would surface
inside the executor's teardown ``finally`` and *mask* the ``CancelledError``,
turning a clean cancel into a failed turn ("[Errno 1] Operation not permitted").
``_terminate_sandbox_proc`` must reap the process group via the setuid helper and
**never raise**, regardless of which signal path fails.
"""
from __future__ import annotations

import asyncio

import pytest

from app.providers import cli_executor
from app.providers.cli_executor import _terminate_sandbox_proc


@pytest.mark.asyncio
async def test_terminate_kills_running_process():
    """A live subprocess (own session, like the executor spawns) is terminated."""
    proc = await asyncio.create_subprocess_exec(
        "sleep", "30", start_new_session=True
    )
    assert proc.returncode is None
    _terminate_sandbox_proc(proc)
    # The group SIGKILL lands; the process exits promptly.
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_terminate_is_noop_for_none_or_finished():
    """No proc / already-exited proc is a safe no-op (no exception)."""
    _terminate_sandbox_proc(None)
    proc = await asyncio.create_subprocess_exec("true")
    await proc.wait()
    _terminate_sandbox_proc(proc)  # returncode set → no-op


@pytest.mark.asyncio
async def test_terminate_swallows_eperm_and_never_masks(monkeypatch):
    """The masking-safety property: even when every signal path raises EPERM
    (the cross-user sandbox case), teardown swallows it and does not raise — so an
    in-flight CancelledError is never replaced by a PermissionError."""
    proc = await asyncio.create_subprocess_exec(
        "sleep", "30", start_new_session=True
    )
    try:
        # Simulate the sandbox cross-user case: the helper is unavailable (reap is a
        # no-op in dev) and both direct signals are denied.
        monkeypatch.setattr(cli_executor.sandbox, "reap", lambda pgid: None)
        monkeypatch.setattr(
            cli_executor.os, "killpg",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")),
        )
        monkeypatch.setattr(
            proc, "kill",
            lambda: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")),
        )
        # Must NOT raise despite every path failing with EPERM.
        _terminate_sandbox_proc(proc)
    finally:
        # Real cleanup so the test doesn't leak a sleeper.
        monkeypatch.undo()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
