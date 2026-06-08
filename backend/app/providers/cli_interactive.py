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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pyte

from app import sandbox
from app.cli_policy import get_policy
from app.config import DeploymentMode, get_settings
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

# Single-shot, read-only meta commands the seam may auto-send in *any* deployment
# mode (D-0016 / P-0019). A run that types only these (and no task prompt) is a
# human-clicked subscription-info query, not autonomous task driving — it's the
# subscription_usage.py + planned `/model` capture path. Anything beyond this set
# (a task prompt, an arbitrary control command) is *autonomous driving* and is
# personal-mode only. Allow-policy still bounds *which* commands are reachable
# at all (TerminalPolicy.allowed_commands); this is the orthogonal mode gate.
_META_COMMANDS = frozenset({"/usage", "/model", "/status", "/cost"})


def _command_head(cmd: str) -> str:
    """First token of a control command — "/usage show" (grok) → "/usage"."""
    return cmd.strip().split()[0] if cmd.strip() else ""


def _is_autonomous_driving(prompt: str, control_commands: list[str]) -> bool:
    """A run is 'autonomous driving' (D-0016 prohibited outside personal mode)
    if it submits a real task prompt or any control command that isn't a
    single-shot meta query. Empty-prompt + meta-only is single-shot and allowed."""
    if prompt.strip():
        return True
    for cmd in control_commands:
        # Match on the head token so "/usage show" (grok) counts as meta.
        if _command_head(cmd) not in _META_COMMANDS:
            return True
    return False


def _is_meta_capture(prompt: str, control_commands: list[str]) -> bool:
    """A sanctioned read-only meta capture: at least one control command, no task
    prompt, and every command is a single-shot meta query (/usage·/status·/cost·
    /model, head-matched so grok's "/usage show" counts). These are safe in every
    mode and DON'T require the terminal-seam master switch (D-0023) — the switch
    gates the wider autonomous TUI surface, enforced separately by the
    personal-mode gate. Per-provider commands all reduce to a meta head:
    claude/agy "/usage", codex "/status", grok "/usage show"."""
    return bool(control_commands) and not _is_autonomous_driving(prompt, control_commands)


# Seconds of silence on the PTY that we treat as "the turn finished". TUIs render
# continuously while working, so a gap this long means it's waiting on us.
_DEFAULT_IDLE_TIMEOUT = 8.0
# How long to let the TUI paint its first frame before we start typing.
_STARTUP_GRACE = 1.5
# After a content-match (capture_until) we let the panel finish painting this long
# before snapshotting — covers a redraw-heavy panel that won't otherwise idle.
_MATCH_SETTLE = 1.5
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


def render_screen(screen: pyte.Screen) -> str:
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

    def __init__(self, provider_def: ProviderDef, instance: ProviderInstance | None = None) -> None:
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
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        binary = self._def.cli_binary
        if not binary:
            yield ExecEvent(
                kind=EventKind.error, message=f"No CLI binary configured for {self.name}"
            )
            return

        extra = extra or {}
        control_commands: list[str] = list(extra.get("control_commands") or [])
        idle_timeout = float(extra.get("idle_timeout") or _DEFAULT_IDLE_TIMEOUT)
        hard_timeout = _settings.run_timeout_seconds
        spec = get_tui_spec(self._def.name)  # per-provider TUI adapter
        # Some TUIs (grok's async credit panel) redraw forever and never go idle,
        # so idle detection alone hits the hard timeout. When the caller knows what
        # the answer looks like it passes `capture_until` — a regex; once it matches
        # the rendered screen we let the panel settle briefly and stop, instead of
        # waiting for an idle gap that never comes (D-0023; grok /usage capture).
        capture_until_raw = extra.get("capture_until")
        capture_until = re.compile(capture_until_raw) if capture_until_raw else None

        policy = get_policy()
        # Read-only meta captures (the /usage·/status·/cost·/model single-shot path,
        # e.g. the subscription-usage poll) are sanctioned in every mode and do NOT
        # require the terminal-seam master switch — they ride the built-in
        # _META_COMMANDS allowlist instead (D-0023). Everything else needs the seam
        # enabled and rides the operator allowlist. The wider autonomous surface is
        # still gated by the personal-mode check below.
        meta_capture = _is_meta_capture(prompt, control_commands)

        if not policy.enabled and not meta_capture:
            yield ExecEvent(
                kind=EventKind.error,
                message=f"[{self.name}] terminal seam disabled (terminal_seam_enabled=false)",
            )
            return

        # Policy gate: every control command must pass BEFORE we spawn anything.
        for cmd in control_commands:
            if meta_capture:
                # Independent of the master switch — restrict to the read-only meta set.
                ok = _command_head(cmd) in _META_COMMANDS
                reason = "not a read-only meta command" if not ok else ""
            else:
                ok, reason = policy.check_command(cmd)
            if not ok:
                logger.warning(
                    "[%s] terminal seam refused control command %r: %s", self.name, cmd, reason
                )
                yield ExecEvent(
                    kind=EventKind.error,
                    message=f"[{self.name}] control command refused: {reason}",
                    data={"command": cmd, "reason": reason},
                )
                return

        # D-0016 mode gate: autonomous full-TTY driving (task prompt or a non-meta
        # control command) is *personal-mode only* — that pattern (bot types in a
        # loop) is the one the web-TTY ToS posture moves off consumer plan CLIs.
        # Single-shot meta queries (the /usage capture path) are allowed in all
        # modes since they're human-clicked, read-only, one-screen reads.
        # `managed` already hard-refuses plan-CLI at config load (config.py §1a),
        # so in practice this gate fires for `oss`.
        if (
            _is_autonomous_driving(prompt, control_commands)
            and _settings.deployment_mode != DeploymentMode.personal
        ):
            mode = _settings.deployment_mode.value
            logger.warning(
                "[%s] terminal seam refused autonomous driving in %s mode (D-0016)",
                self.name, mode,
            )
            yield ExecEvent(
                kind=EventKind.error,
                message=(
                    f"[{self.name}] autonomous full-TTY driving is personal-mode only "
                    f"(DEPLOYMENT_MODE={mode}); use headless `cli -p`, the web-TTY UX, "
                    f"or single-shot meta commands ({sorted(_META_COMMANDS)})"
                ),
                data={"mode": mode, "reason": "D-0016 autonomous driving"},
            )
            return

        launch = self._build_launch(allow_shell=policy.allow_shell)

        env = os.environ.copy()
        if self._instance and self._instance.cli_config_dir and self._instance.cli_config_env:
            env[self._instance.cli_config_env] = self._instance.cli_config_dir
        # Force a predictable terminal so the TUI emits a stable escape vocabulary.
        env["TERM"] = "xterm-256color"

        yield ExecEvent(
            kind=EventKind.log,
            message=(
                f"[{self.name}] PTY seam launching: {' '.join(launch)} "
                f"(allow_shell={policy.allow_shell})"
            ),
        )
        yield ExecEvent(kind=EventKind.phase, phase="running")

        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, _PTY_ROWS, _PTY_COLS)

        proc: asyncio.subprocess.Process | None = None
        accumulated: list[str] = []
        try:
            # Privilege drop: run the TUI as the low-priv `sandbox` user via the
            # setuid helper (P-0022/D-0020). No-op outside the container.
            proc = await asyncio.create_subprocess_exec(
                *sandbox.wrap(launch),
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

            async def _drain_until_idle(
                *, capture: bool, first_timeout: float = idle_timeout
            ) -> None:
                """Read PTY output until it goes idle, feeding every byte into the
                emulated screen. When capture, snapshot the final rendered screen
                (the TUI's response) once idle; otherwise just advance the screen
                state (startup banner / echoed input we don't keep).

                If `capture_until` is set, a content-match short-circuits idle
                detection: once the rendered screen matches, let it settle briefly,
                then snapshot and stop — for panels that redraw forever (grok)."""
                nonlocal accumulated
                timeout = first_timeout
                while True:
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                    except TimeoutError:
                        break  # idle → turn done
                    if not chunk:
                        break  # EOF
                    timeout = idle_timeout
                    vt_stream.feed(chunk)
                    if capture and capture_until is not None and capture_until.search(
                        render_screen(screen)
                    ):
                        # The answer is on screen; don't wait for an idle gap that
                        # may never come. Let it finish painting, then stop.
                        await asyncio.sleep(_MATCH_SETTLE)
                        break
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
            except TimeoutError:
                yield ExecEvent(
                    kind=EventKind.error,
                    message=f"[{self.name}] PTY seam timeout after {hard_timeout}s",
                )
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
            yield ExecEvent(
                kind=EventKind.error, message=f"[{self.name}] binary not found: {binary}"
            )
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
                # The TUI runs as `sandbox`, so batond can't signal it cross-user;
                # reap the process group through the setuid helper (pgid == pid via
                # start_new_session). Direct killpg/kill below covers the un-split case.
                sandbox.reap(proc.pid)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (TimeoutError, Exception):
                    pass
