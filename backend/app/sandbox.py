"""
sandbox.py — privilege-drop wiring for agent subprocesses (P-0022 / D-0020).

The backend runs as the control-plane user `batond`; every agent CLI must run as
the low-privilege `sandbox` user so kernel DAC fences it off from /app and
control-plane /data. A non-root parent cannot `setuid`, so the drop goes through
the setuid helper `sandbox-spawn` (built in Dockerfile.base, installed
4750 root:batond). This module is the ONE place that wires it in — the headless
executor (`cli_executor`), the web-TTY PTY seam (`pty_session`), and the API-path
`code_exec` tool all call `wrap()` so the privilege drop cannot be bypassed.

When the helper is absent (local dev, unit tests, non-container hosts) `wrap()`
returns the command unchanged so the same code path runs un-sandboxed locally —
UNLESS `REQUIRE_SANDBOX` is set (the container image), in which case `wrap()` fails
closed (`SandboxUnavailableError`) rather than degrading to an un-sandboxed spawn.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()


class SandboxUnavailableError(RuntimeError):
    """A sandboxed launch was required but the spawner is unavailable.

    Raised by `wrap()` under `REQUIRE_SANDBOX` so the privilege drop fails CLOSED:
    we never silently run agent CLIs or API-path `code_exec` as the control-plane
    `batond` user when isolation was promised (the P-0046 non-sandbox bug)."""


def available() -> bool:
    """True when the setuid spawner is present and executable (i.e. in-container)."""
    path = _settings.sandbox_spawn_path
    return bool(path) and os.access(path, os.X_OK)


def required() -> bool:
    """Whether sandboxing is MANDATORY in this deployment (set in the container
    image via `REQUIRE_SANDBOX`). When True, `wrap()` refuses rather than
    degrading to a direct un-sandboxed spawn if the spawner is missing."""
    return bool(_settings.require_sandbox)


def wrap(cmd: list[str]) -> list[str]:
    """Prefix `cmd` so it runs as the `sandbox` user via the setuid helper.

    Fails CLOSED when `REQUIRE_SANDBOX` is set but the spawner is unavailable —
    raises `SandboxUnavailableError` rather than running un-sandboxed. Otherwise
    (local dev / tests, no spawner) it's a no-op and returns `cmd` unchanged so the
    same code path runs without the container's privilege split.
    """
    if not available():
        if required():
            raise SandboxUnavailableError(
                f"sandbox spawner {_settings.sandbox_spawn_path!r} is unavailable but "
                "REQUIRE_SANDBOX is set — refusing to run un-sandboxed as the "
                "control-plane user"
            )
        logger.debug("[sandbox] spawner unavailable — running %s un-sandboxed", cmd[:1])
        return cmd
    return [_settings.sandbox_spawn_path, "--", *cmd]


def git_trust_env(workdir: str | None) -> dict[str, str]:
    """Per-process git trust for one agent workspace (mixed-uid workspaces).

    The workspace `.git` is created by the control-plane `batond` user while agent
    processes run as the `sandbox` user, so every agent `git` invocation trips
    git's dubious-ownership check (`safe.directory`) — agents then either follow
    the error's suggestion and mutate their own global gitconfig, or misread the
    workspace as broken. Scope the exception to exactly this workspace via git's
    environment-based config (GIT_CONFIG_COUNT/KEY_n/VALUE_n, git ≥ 2.31); never
    a global `safe.directory=*` off-switch, which would also trust repos planted
    in shared directories like /tmp.
    """
    if not workdir:
        return {}
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        # git compares the repo's real path; a symlinked workdir must not miss.
        "GIT_CONFIG_VALUE_0": os.path.realpath(workdir),
    }


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


async def read_file_as_agent(path: str, *, max_bytes: int) -> bytes | None:
    """Read a file *as the sandbox user* and return its bytes, or None.

    CLI agents (e.g. antigravity/agy) save generated artifacts under their own HOME
    (`/home/agent/...`), which `batond` cannot traverse — so the backend can't read
    them directly even when the files are world-readable, because a parent dir is
    not. Route the read through the same setuid spawner the CLIs use so it runs as
    `sandbox`. Streams via `cat`, capped at `max_bytes` (returns None if exceeded or
    on any error). Falls back to a direct read when the spawner is unavailable
    (dev / tests / non-container)."""
    if not available():
        try:
            if os.path.getsize(path) > max_bytes:
                return None
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            _settings.sandbox_spawn_path, "--", "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        def _kill() -> None:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        async def _drain() -> bytes | None:
            # Read in chunks so we keep draining the pipe — a single read(n) + wait()
            # deadlocks once `cat`'s output exceeds the ~64K pipe buffer (it blocks on
            # write while we block on wait()). Enforce the cap as we go; on over-cap
            # kill `cat` rather than leaving it blocked on the unread tail.
            assert proc is not None and proc.stdout is not None
            buf = bytearray()
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    _kill()
                    return None
            return bytes(buf)

        data = await asyncio.wait_for(_drain(), timeout=120)
        if data is None:
            await asyncio.wait_for(proc.wait(), timeout=10)
            return None
        rc = await asyncio.wait_for(proc.wait(), timeout=10)
        return data if rc == 0 and data else None
    except Exception as exc:  # noqa: BLE001 — best-effort read must not raise (incl. TimeoutError)
        logger.warning("[sandbox] read_file_as_agent(%s) failed: %s", path, exc)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return None


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
