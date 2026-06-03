"""
console.py — Scoped in-UI console actions (set models, run auth).

Security model (see Settings.web_console_available):
  - Off unless ENABLE_WEB_CONSOLE=true AND WEB_CONSOLE_TOKEN is set.
  - Never available in DEPLOYMENT_MODE=managed.
  - Every console request must present the token.

This is NOT a general shell. The only process it spawns is the project's own
auth.sh, with a single validated provider/instance argument — so it cannot run
arbitrary commands. It runs that login flow under a PTY and streams its output
over a WebSocket, forwarding the user's keystrokes to that one process so
interactive logins (device codes, redirect URLs) can be completed from the UI.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

AUTH_SCRIPT = "/app/scripts/auth.sh"


def valid_auth_target(target: str) -> bool:
    """A target is a known template or a declared instance id — nothing else."""
    from app.providers.registry import ALL_TEMPLATE_NAMES, get_instance

    template = target.split(":", 1)[0]
    if template not in ALL_TEMPLATE_NAMES:
        return False
    if ":" in target:
        return get_instance(target) is not None
    return True


class PtyAuthSession:
    """Runs `auth.sh <target>` under a PTY, bridging it to a WebSocket."""

    def __init__(self, target: str) -> None:
        self.target = target
        self._proc: Optional[subprocess.Popen] = None
        self._master_fd: Optional[int] = None
        self._pts_minor: Optional[int] = None

    async def start(self, rows: int = 30, cols: int = 100) -> None:
        loop = asyncio.get_running_loop()
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)  # /dev/pts/N
        try:
            self._pts_minor = int(slave_name.rsplit("/", 1)[1])
        except ValueError:
            self._pts_minor = None
        # start_new_session makes bash the session leader with this pts as its
        # controlling terminal; children inherit that tty, which is how we find
        # and kill the whole login (even when a CLI re-parents itself).
        self._proc = subprocess.Popen(
            ["bash", AUTH_SCRIPT, self.target],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            start_new_session=True, env=os.environ.copy(), close_fds=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd
        os.set_blocking(master_fd, False)
        self.resize(rows=rows, cols=cols)
        self._loop = loop

    def resize(self, rows: int, cols: int) -> None:
        if self._master_fd is None:
            return
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    async def read(self) -> Optional[bytes]:
        """Await the next chunk of output; None when the process has exited."""
        # Capture the fd in a local: close() may null self._master_fd while this
        # read is in flight (cancellation race). Using the captured integer keeps
        # add_reader/remove_reader symmetric so the reader is always unregistered —
        # dereferencing self._master_fd here would pass None to remove_reader after
        # close(), raising ValueError and leaving a dangling reader on the closed
        # fd that uvloop re-fires forever.
        fd = self._master_fd
        if fd is None:
            return None
        fut: asyncio.Future = self._loop.create_future()

        def _on_readable() -> None:
            self._loop.remove_reader(fd)
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not fut.done():
                fut.set_result(data)

        self._loop.add_reader(fd, _on_readable)
        try:
            data = await fut
        finally:
            try:
                self._loop.remove_reader(fd)
            except Exception:
                pass
        return data or None

    def write(self, data: str) -> None:
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, data.encode("utf-8", "ignore"))
            except OSError:
                pass

    def exit_code(self) -> Optional[int]:
        if self._proc is None:
            return None
        return self._proc.poll()

    def close(self) -> None:
        # Kill every process whose controlling terminal is our pts — this catches
        # the login CLI even when it re-parents into its own session/group, which
        # process-group or PPID-tree kills miss.
        if self._pts_minor is not None:
            for pid in _pids_on_pts(self._pts_minor):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.kill()
                self._proc.wait(timeout=2)  # reap the wrapper
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        if self._master_fd is not None:
            fd = self._master_fd
            self._master_fd = None
            # Unregister before closing: a reader left on a closed fd is re-fired
            # by uvloop indefinitely. Guard for close() being called before start().
            loop = getattr(self, "_loop", None)
            if loop is not None:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


def _pids_on_pts(pts_minor: int) -> list[int]:
    """All pids whose controlling terminal is /dev/pts/<pts_minor>."""
    # Linux tty_nr encoding for UNIX98 pts (major 136): (136<<8)|(minor & 0xff)
    # plus high minor bits; for minor < 256 this is simply 0x8800 | minor.
    target = (136 << 8) | (pts_minor & 0xFF) | ((pts_minor & 0xFFF00) << 12)
    pids: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            tty_nr = int(fields[4])  # tty_nr is the 7th overall field
        except (OSError, IndexError, ValueError):
            continue
        if tty_nr == target:
            pids.append(int(entry))
    return pids
