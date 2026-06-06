"""
providers/cli_interactive.py — PTY interactive-CLI seam (D-0015 / P-0018).

The headless seam (cli_executor.py:CLIExecutor) runs `cli -p "<prompt>"` and reads
a structured stream. That can't drive interactive-only surfaces: probed live
(2026-06-06) only `claude -p "/usage"` returns anything; grok/agy block and
codex `-p` means `--profile`. This seam instead spawns the *real* TUI inside a
pseudo-terminal, types the prompt and control commands, renders the ANSI stream
through a virtual-terminal screen buffer (pyte), and parses the final rendered
screen into ExecEvents — same Executor interface as the headless path.

Why an emulated screen rather than concatenating stripped chunks: TUIs that
redraw continuously (grok's async credit panel especially) bury their final
content under a stream of full-screen repaints, so naive concatenation captures
noise. Feeding the raw bytes into pyte applies the cursor moves/clears exactly as
a real terminal would, leaving only the *final* on-screen state to read back.

Because a PTY-driven TUI is a much wider surface than `cli -p`, every control
command the seam sends is checked against the config allow-policy
(app/cli_policy.py) first, and policy.allow_shell decides whether the CLI is
launched with its skip-permission flag (so model-emitted shell stays gated when
off). Default posture is closed; a denied command is refused, logged, and turned
into an `error` event rather than sent.

Driving inputs (via run_stream `extra`):
  extra["control_commands"]: list[str] — slash commands to send into the TUI in
      order (each policy-checked). The main `prompt` is typed first when non-empty.
  extra["idle_timeout"]: float — seconds of no PTY output that mark a turn done
      (default _DEFAULT_IDLE_TIMEOUT). The overall cap is run_timeout_seconds.

Completion of a TUI turn has no clean machine signal, so we use output-idle
detection bounded by the hard run timeout.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import termios
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import pyte

from app.cli_policy import get_policy
from app.config import get_settings
from app.providers.base import (
    EventKind,
    ExecEvent,
    ExecResult,
    Executor,
    Usage,
)
from app.providers.registry import ProviderDef, ProviderInstance

logger = logging.getLogger(__name__)
_settings = get_settings()

# Seconds of silence on the PTY that we treat as "the turn finished". TUIs render
# continuously while working, so a gap this long means it's waiting on us.
_DEFAULT_IDLE_TIMEOUT = 8.0
# How long to let the TUI paint its first frame before we start typing.
_STARTUP_GRACE = 1.5
# PTY window size we advertise so the TUI lays out without truncation.
_PTY_ROWS, _PTY_COLS = 50, 200

# Strip ANSI/VT100 control sequences so scraped output is plain text.
#   CSI ... letter | OSC ... BEL/ST | single-char escapes | charset selects
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI sequences (colours, cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (titles) ending BEL/ST
    r"|\x1b[()][AB0-2]"               # charset selection
    r"|\x1b[=>]"                       # keypad mode
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f]"  # stray control chars (keep \t \n \r)
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and stray control chars from PTY output."""
    return _ANSI_RE.sub("", text)


def render_screen(screen: "pyte.Screen") -> str:
    """Read back the final rendered state of an emulated terminal screen.

    pyte pads every line to the full screen width and keeps cleared rows blank,
    so we rstrip each line and trim leading/trailing blank rows — leaving the
    panel content as it actually appears, redraws already collapsed into the
    final frame.

    For a HistoryScreen we prepend the scrollback (lines that scrolled off the
    top) so output longer than the viewport is captured when the TUI scrolls via
    linefeeds. Caveat: TUIs that repaint a fixed window with cursor addressing
    (grok pins an input box and redraws the transcript above it) never transmit
    scrolled-off lines, so history stays empty and only the painted window is
    recoverable — there are no bytes to reconstruct the rest from. This matters
    only for long-form task output; the /usage panels this seam exists for fit on
    one screen and capture in full."""
    lines: list[str] = []
    history = getattr(screen, "history", None)
    if history is not None:
        cols = screen.columns
        for line in history.top:
            lines.append("".join(line[x].data for x in range(cols)).rstrip())
    lines.extend(ln.rstrip() for ln in screen.display)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# ── Per-provider TUI adapters ──────────────────────────────────────────────────
# Each CLI's TUI behaves differently (probed live 2026-06-06): claude takes a
# typed command + Enter directly; grok opens a startup modal that must be
# dismissed first; agy is autocomplete-menu-driven (type filters, Enter selects);
# codex needs the typed text to render before Enter registers as submit. A spec
# captures those differences so the generic driver stays uniform.

_ESC = "\x1b"
_ENTER = "\r"


@dataclass(frozen=True)
class TUISpec:
    # Seconds to let the first frame paint before we touch the keyboard.
    startup_grace: float = _STARTUP_GRACE
    # Keystrokes to clear a startup modal/dialog before the prompt is usable.
    startup_keys: tuple[str, ...] = ()
    # Pause after typing a command so the TUI renders it (and any autocomplete
    # menu) before we submit — without this, Enter races ahead of the render.
    type_settle: float = 0.0
    # The keystroke that submits a typed command.
    submit: str = _ENTER
    # Extra keystrokes after submit (e.g. a second Enter to confirm a menu pick).
    post_submit_keys: tuple[str, ...] = ()


# Default spec works for claude. Others override only what differs.
_TUI_SPECS: dict[str, TUISpec] = {
    "claude": TUISpec(),
    # Dismiss the "New worktree / Resume / Quit" startup modal, then settle so the
    # typed command lands in the prompt rather than the dialog.
    "grok": TUISpec(startup_grace=3.0, startup_keys=(_ESC,), type_settle=1.0),
    # Autocomplete menu: type filters the list, Enter selects + runs the match.
    "agy": TUISpec(startup_grace=2.0, type_settle=1.0),
    # Enter only registers once the typed text has rendered.
    "codex": TUISpec(startup_grace=2.0, type_settle=1.0),
}


def get_tui_spec(provider: str) -> TUISpec:
    return _TUI_SPECS.get(provider, TUISpec())


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


class CLIInteractiveExecutor(Executor):
    """
    Drives an official CLI's interactive TUI inside a pseudo-terminal.

    Parallel to CLIExecutor (same run_stream contract); selected per-instance via
    the exec-seam override in the registry. Never reads, stores, or forwards OAuth
    tokens — the binary owns its own auth via its config dir, exactly as headless.
    """

    def __init__(self, provider_def: ProviderDef, instance: Optional["ProviderInstance"] = None) -> None:
        self._def = provider_def
        self._instance = instance
        self.name = instance.id if instance else provider_def.name
        self.tier = "agent"

    @property
    def kind(self) -> str:
        return "cli"

    def is_healthy(self) -> bool:
        import shutil
        return bool(self._def.cli_binary and shutil.which(self._def.cli_binary))

    def _build_launch(self, *, allow_shell: bool) -> list[str]:
        """Launch argv for the interactive TUI (no -p). allow_shell decides whether
        the CLI may auto-run model-emitted shell/tools."""
        binary = self._def.cli_binary or ""
        cmd = [binary]
        if not allow_shell:
            return cmd
        # Only when the policy explicitly permits auto-run do we pass the
        # skip-permission flag. Flag name differs per binary.
        skip_flag = {
            "claude": "--dangerously-skip-permissions",
            "grok": "--always-approve",
            "agy": "--dangerously-skip-permissions",
            "codex": "--dangerously-bypass-approvals-and-sandbox",
        }.get(binary)
        if skip_flag:
            cmd.append(skip_flag)
        return cmd

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
        binary = self._def.cli_binary
        if not binary:
            yield ExecEvent(kind=EventKind.error, message=f"No CLI binary configured for {self.name}")
            return

        policy = get_policy()
        if not policy.enabled:
            yield ExecEvent(
                kind=EventKind.error,
                message=f"[{self.name}] terminal seam disabled (terminal_seam_enabled=false)",
            )
            return

        extra = extra or {}
        control_commands: list[str] = list(extra.get("control_commands") or [])
        idle_timeout = float(extra.get("idle_timeout") or _DEFAULT_IDLE_TIMEOUT)
        hard_timeout = _settings.run_timeout_seconds
        spec = get_tui_spec(self._def.name)  # per-provider TUI adapter

        # Policy gate: every control command must pass BEFORE we spawn anything.
        for cmd in control_commands:
            ok, reason = policy.check_command(cmd)
            if not ok:
                logger.warning("[%s] terminal seam refused control command %r: %s", self.name, cmd, reason)
                yield ExecEvent(
                    kind=EventKind.error,
                    message=f"[{self.name}] control command refused: {reason}",
                    data={"command": cmd, "reason": reason},
                )
                return

        launch = self._build_launch(allow_shell=policy.allow_shell)

        env = os.environ.copy()
        if self._instance and self._instance.cli_config_dir and self._instance.cli_config_env:
            env[self._instance.cli_config_env] = self._instance.cli_config_dir
        # Force a predictable terminal so the TUI emits a stable escape vocabulary.
        env["TERM"] = "xterm-256color"

        yield ExecEvent(kind=EventKind.log,
                        message=f"[{self.name}] PTY seam launching: {' '.join(launch)} (allow_shell={policy.allow_shell})")
        yield ExecEvent(kind=EventKind.phase, phase="running")

        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, _PTY_ROWS, _PTY_COLS)

        proc: Optional[asyncio.subprocess.Process] = None
        accumulated: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *launch,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=workdir,
                env=env,
                start_new_session=True,  # own process group so we can signal the whole TUI
            )
            os.close(slave_fd)
            slave_fd = -1

            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader()
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader),
                os.fdopen(master_fd, "rb", buffering=0),
            )

            # Emulated terminal: raw PTY bytes are fed here so cursor moves and
            # screen clears are applied exactly as a real terminal would, instead
            # of being concatenated. We read the rendered screen back on capture.
            # HistoryScreen retains scrollback so output longer than the viewport
            # is captured for linefeed-scrolling TUIs (repaint-style TUIs like
            # grok only ever transmit their painted window — see render_screen).
            screen = pyte.HistoryScreen(_PTY_COLS, _PTY_ROWS, history=5000, ratio=0.5)
            vt_stream = pyte.ByteStream(screen)

            def _write(s: str) -> None:
                os.write(master_fd, s.encode("utf-8"))

            async def _drain_until_idle(*, capture: bool, first_timeout: float = idle_timeout) -> None:
                """Read PTY output until it goes idle, feeding every byte into the
                emulated screen. When capture, snapshot the final rendered screen
                (the TUI's response) once idle; otherwise just advance the screen
                state (startup banner / echoed input we don't keep)."""
                nonlocal accumulated
                timeout = first_timeout
                while True:
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                    except asyncio.TimeoutError:
                        break  # idle → turn done
                    if not chunk:
                        break  # EOF
                    timeout = idle_timeout
                    vt_stream.feed(chunk)
                if capture:
                    snapshot = render_screen(screen)
                    if snapshot:
                        accumulated.append(snapshot)

            async def _send(text: str) -> None:
                """Type text, let the TUI render it, then submit per the spec."""
                _write(text)
                if spec.type_settle:
                    await asyncio.sleep(spec.type_settle)
                _write(spec.submit)
                for k in spec.post_submit_keys:
                    _write(k)

            # Bound the whole interaction by the hard run timeout.
            async def _interact() -> None:
                # Let the TUI paint + settle; discard the banner.
                await _drain_until_idle(capture=False, first_timeout=spec.startup_grace)
                # Clear any startup modal/dialog so input lands in the prompt.
                for k in spec.startup_keys:
                    _write(k)
                    await _drain_until_idle(capture=False)
                if prompt.strip():
                    await _send(prompt)
                    await _drain_until_idle(capture=True)
                for cmd in control_commands:
                    # Already policy-checked above; safe to send.
                    await _send(cmd)
                    await _drain_until_idle(capture=True)

            try:
                async with asyncio.timeout(hard_timeout):
                    await _interact()
            except asyncio.TimeoutError:
                yield ExecEvent(kind=EventKind.error, message=f"[{self.name}] PTY seam timeout after {hard_timeout}s")
                return
            finally:
                transport.close()

            # Surface the rendered screen(s) as a single token + a result event.
            text = "\n".join(accumulated)
            if text:
                yield ExecEvent(kind=EventKind.token, text=text)
            usage = Usage()  # subscription TUI — no per-token charge metered here
            result = ExecResult(text=text, usage=usage, provider=self.name, model=binary)
            yield ExecEvent(
                kind=EventKind.result,
                message=f"[{self.name}] PTY seam complete",
                data={"result": result, "usage": usage.__dict__},
            )

        except FileNotFoundError:
            yield ExecEvent(kind=EventKind.error, message=f"[{self.name}] binary not found: {binary}")
        except Exception as exc:
            logger.exception("[%s] PTY seam error", self.name)
            yield ExecEvent(kind=EventKind.error, message=f"[{self.name}] {exc}")
        finally:
            if slave_fd >= 0:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if proc and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
