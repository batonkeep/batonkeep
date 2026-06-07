"""
pty_session.py — Generic PTY ↔ caller bridge for human-driven terminal sessions.

This is the careful part of the web-TTY lane (D-0016 seam #3 / D-0017): spawn one
validated command under a pseudo-terminal, stream its output, forward the user's
keystrokes back in, and tear the whole process tree down cleanly. It carries no
policy of its own — *what* may be spawned (auth.sh, a provider TUI) is decided by
the caller; this class only runs the given argv and bridges the bytes.

Two consumers build on it:
  - PtyAuthSession (console.py) — runs the fixed auth.sh login flow.
  - WebTtySession (web_tty.py) — runs a provider's interactive CLI in a session
    workspace, human-in-the-loop (the ToS-clean interactive lane).

The fd / event-loop-reader / pts-kill handling here is subtle and was hardened in
the auth console; keep it in one place so both lanes inherit the same fixes.
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

from app import sandbox

logger = logging.getLogger(__name__)


class PtySession:
    """Runs one argv under a PTY, bridging it to an async caller (e.g. a WebSocket).

    The command, working directory and environment are supplied by the caller —
    this class owns only the PTY lifecycle and byte plumbing.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env if env is not None else os.environ.copy()
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._pts_minor: int | None = None

    async def start(self, rows: int = 30, cols: int = 100) -> None:
        loop = asyncio.get_running_loop()
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)  # /dev/pts/N
        try:
            self._pts_minor = int(slave_name.rsplit("/", 1)[1])
        except ValueError:
            self._pts_minor = None
        # start_new_session makes the child the session leader with this pts as its
        # controlling terminal; descendants inherit that tty, which is how close()
        # finds and kills the whole tree (even when a CLI re-parents itself).
        # Privilege drop: run the TUI / auth flow as the low-priv `sandbox` user
        # via the setuid helper (P-0022/D-0020). A clean setuid+execve inside the
        # helper preserves the controlling tty set up here. No-op outside the
        # container, where the helper is absent.
        argv = sandbox.wrap(self._argv)
        self._proc = subprocess.Popen(
            argv,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=self._cwd,
            start_new_session=True, env=self._env, close_fds=True,
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

    async def read(self) -> bytes | None:
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

    def exit_code(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    def close(self) -> None:
        # Privileged reaper: the TUI tree runs as `sandbox`, so batond cannot
        # signal it cross-user (os.kill → PermissionError). start_new_session made
        # the child a session/group leader, so its pgid == its pid; kill that group
        # through the setuid helper (P-0022/D-0020). No-op outside the container.
        if self._proc is not None:
            sandbox.reap(self._proc.pid)
        # Kill every process whose controlling terminal is our pts — this catches
        # a CLI even when it re-parents into its own session/group, which
        # process-group or PPID-tree kills miss. (Direct kill works when un-split;
        # cross-user it raises PermissionError and the helper above did the work.)
        if self._pts_minor is not None:
            for pid in _pids_on_pts(self._pts_minor):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
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
