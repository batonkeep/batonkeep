"""
sandbox.py — privilege-drop wiring for agent subprocesses (P-0022 / D-0020).

The backend runs as the control-plane user `batond`; every agent CLI must run as
the low-privilege `sandbox` user so kernel DAC fences it off from /app and
control-plane /data. A non-root parent cannot `setuid`, so the drop goes through
the setuid helper `sandbox-spawn` (built in Dockerfile.base, installed
4750 root:batond). This module is the ONE place that wires it in — both the
headless executor (`cli_executor`) and the web-TTY PTY seam (`pty_session`) call
`wrap()` so the privilege drop cannot be bypassed.

When the helper is absent (local dev, unit tests, non-container hosts) `wrap()`
returns the command unchanged so the same code path runs un-sandboxed locally.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()


def available() -> bool:
    """True when the setuid spawner is present and executable (i.e. in-container)."""
    path = _settings.sandbox_spawn_path
    return bool(path) and os.access(path, os.X_OK)


def wrap(cmd: list[str]) -> list[str]:
    """Prefix `cmd` so it runs as the `sandbox` user via the setuid helper.

    No-op (returns `cmd` unchanged) when the helper is unavailable, so local/dev
    runs work without the container's privilege split.
    """
    if not available():
        logger.debug("[sandbox] spawner unavailable — running %s un-sandboxed", cmd[:1])
        return cmd
    return [_settings.sandbox_spawn_path, "--", *cmd]


async def path_exists(path: str) -> bool:
    """Whether `path` exists *from the sandbox user's vantage point*.

    Plan-CLI auth dirs live under the sandbox user's HOME (/home/agent), which the
    control-plane `batond` user cannot traverse — so a direct ``os.path.exists`` from
    the backend wrongly reports them missing (every provider shows "offline" even
    though the CLIs are logged in). Route the check through the same setuid spawner
    the CLIs use so it runs as `sandbox`. Falls back to a direct check when the
    spawner is unavailable (dev / tests / non-container)."""
    if not available():
        return os.path.exists(path)
    try:
        proc = await asyncio.create_subprocess_exec(
            _settings.sandbox_spawn_path, "--", "test", "-e", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except Exception as exc:  # noqa: BLE001 — best-effort probe must not raise
        logger.warning("[sandbox] path_exists(%s) failed: %s", path, exc)
        return False


def reap(pgid: int) -> None:
    """SIGKILL a sandbox-owned process group via the privileged helper.

    The web-TTY reaper: `batond` cannot signal `sandbox` processes cross-user, so
    `PtySession.close()` routes the kill through the setuid helper. No-op when the
    helper is unavailable (the caller's own `os.kill` covers the un-split case).
    """
    if not available() or pgid <= 1:
        return
    try:
        subprocess.run(
            [_settings.sandbox_spawn_path, "--reap", str(pgid)],
            check=False,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort teardown must not raise
        logger.warning("[sandbox] reap pgid=%s failed: %s", pgid, exc)
