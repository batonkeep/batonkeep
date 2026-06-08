"""
test_cli_interactive.py — D-0015 PTY interactive-CLI seam (slice 2).

Covers the parts that don't need a live TUI: ANSI scraping, allow_shell launch
flags, and the policy gate (disabled seam + denied control command refuse before
anything is spawned).
"""
from __future__ import annotations

import pytest

from app.cli_policy import TerminalPolicy
from app.providers.base import EventKind
import pyte

from app.providers.cli_interactive import (
    CLIInteractiveExecutor,
    TUISpec,
    get_tui_spec,
    render_screen,
    strip_ansi,
)
from app.providers.registry import ProviderDef, ProviderInstance


def _executor(binary: str = "claude") -> CLIInteractiveExecutor:
    pdef = ProviderDef(name=binary, kind="cli", tier="agent", cli_binary=binary)
    inst = ProviderInstance(id=binary, template=binary, label=binary)
    return CLIInteractiveExecutor(pdef, instance=inst)


async def _collect(executor, *, prompt: str = "hi", **kw):
    return [ev async for ev in executor.run_stream(prompt, workdir="/tmp", **kw)]


class TestStripAnsi:
    def test_strips_colour_codes(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_strips_cursor_moves_and_osc(self):
        assert strip_ansi("\x1b[2J\x1b[H\x1b]0;title\x07text") == "text"

    def test_keeps_newlines_and_tabs(self):
        assert strip_ansi("a\tb\nc") == "a\tb\nc"


class TestRenderScreen:
    def _feed(self, *chunks: bytes) -> str:
        screen = pyte.Screen(40, 6)
        stream = pyte.ByteStream(screen)
        for c in chunks:
            stream.feed(c)
        return render_screen(screen)

    def test_renders_final_frame_after_redraw(self):
        # A redraw-heavy panel: home + clear, then the real content. Naive
        # concatenation would keep the stale frame; the screen keeps only the last.
        out = self._feed(
            b"\x1b[H\x1b[2Jloading...",
            b"\x1b[H\x1b[2JCredits used: 2,500 / 10,000",
        )
        assert out == "Credits used: 2,500 / 10,000"

    def test_trims_blank_padding(self):
        out = self._feed(b"\x1b[2J\x1b[3;1Hmiddle line")
        assert out == "middle line"

    def test_history_screen_captures_scrollback(self):
        # Output longer than the viewport (a full task report, not a one-screen
        # panel) must be captured in full, not truncated to the final screenful.
        screen = pyte.HistoryScreen(20, 3, history=100, ratio=0.5)
        stream = pyte.ByteStream(screen)
        for i in range(8):
            stream.feed(f"line{i}\r\n".encode())
        out = render_screen(screen)
        assert out.splitlines() == [f"line{i}" for i in range(8)]


class TestLaunchFlags:
    def test_no_skip_flag_when_shell_disallowed(self):
        ex = _executor("claude")
        assert ex._build_launch(allow_shell=False) == ["claude"]

    def test_skip_flag_when_shell_allowed(self):
        ex = _executor("claude")
        assert ex._build_launch(allow_shell=True) == ["claude", "--dangerously-skip-permissions"]

    def test_grok_uses_always_approve(self):
        ex = _executor("grok")
        assert ex._build_launch(allow_shell=True) == ["grok", "--always-approve"]


class TestTUISpec:
    def test_known_providers_have_specs(self):
        for p in ("claude", "grok", "agy", "codex"):
            assert isinstance(get_tui_spec(p), TUISpec)

    def test_unknown_provider_gets_default(self):
        assert get_tui_spec("nope") == TUISpec()

    def test_grok_dismisses_startup_modal(self):
        # grok opens a startup dialog that must be cleared before input lands.
        assert get_tui_spec("grok").startup_keys == ("\x1b",)

    def test_menu_driven_clis_settle_before_submit(self):
        # agy/codex need the typed text to render before Enter registers.
        assert get_tui_spec("agy").type_settle > 0
        assert get_tui_spec("codex").type_settle > 0

    def test_claude_default_no_settle(self):
        # claude takes a command + Enter directly.
        assert get_tui_spec("claude").type_settle == 0.0


class TestPolicyGate:
    @pytest.mark.asyncio
    async def test_disabled_seam_refuses_autonomous_before_spawn(self, monkeypatch):
        # A task prompt is autonomous driving — the master switch still gates it.
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=False, allowed_commands=frozenset({"/usage"})),
        )
        evs = await _collect(_executor(), prompt="do the thing", extra={"control_commands": ["/usage"]})
        assert evs[-1].kind == EventKind.error
        assert "disabled" in evs[-1].message

    @pytest.mark.asyncio
    async def test_disabled_seam_refuses_non_meta_command(self, monkeypatch):
        # Empty prompt but a non-meta control command is still autonomous → gated.
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=False, allowed_commands=frozenset({"/clear"})),
        )
        evs = await _collect(_executor(), prompt="", extra={"control_commands": ["/clear"]})
        assert evs[-1].kind == EventKind.error
        assert "disabled" in evs[-1].message

    @pytest.mark.asyncio
    async def test_disabled_seam_allows_meta_capture(self, monkeypatch):
        # D-0023 / A: a read-only meta capture (empty prompt + meta command only)
        # bypasses the master switch — even with the seam off and an empty operator
        # allowlist. It rides the built-in _META_COMMANDS set and proceeds to spawn.
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=False, allowed_commands=frozenset()),
        )

        async def _boom(*a, **k):  # don't actually launch a TUI in the test
            raise FileNotFoundError("no binary")

        monkeypatch.setattr(
            "app.providers.cli_interactive.asyncio.create_subprocess_exec", _boom
        )
        # grok's "/usage show" exercises head-matching on a multi-token command.
        evs = await _collect(_executor("grok"), prompt="", extra={"control_commands": ["/usage show"]})
        assert any("PTY seam launching" in (e.message or "") for e in evs)  # passed the gate
        assert not any(
            e.kind == EventKind.error and "disabled" in (e.message or "") for e in evs
        )

    @pytest.mark.asyncio
    async def test_denied_control_command_refused_before_spawn(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"})),
        )
        evs = await _collect(_executor(), extra={"control_commands": ["/exec rm -rf /"]})
        err = evs[-1]
        assert err.kind == EventKind.error
        assert "refused" in err.message
        assert err.data["command"] == "/exec rm -rf /"

    @pytest.mark.asyncio
    async def test_missing_binary_errors(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"})),
        )
        pdef = ProviderDef(name="x", kind="cli", tier="agent", cli_binary=None)
        ex = CLIInteractiveExecutor(pdef, instance=ProviderInstance(id="x", template="x", label="x"))
        evs = await _collect(ex)
        assert evs[-1].kind == EventKind.error
        assert "No CLI binary" in evs[-1].message


# Helper to drive the D-0016 mode gate without touching the global settings
# cache: just stub the deployment_mode on the seam's settings handle.
def _set_mode(monkeypatch, mode: str) -> None:
    # Settings is a frozen pydantic model; mutate the field in its __dict__ via
    # setitem so monkeypatch auto-restores it (mirrors the orchestrator tests).
    from app.config import DeploymentMode
    from app.providers import cli_interactive as ci
    monkeypatch.setitem(ci._settings.__dict__, "deployment_mode", DeploymentMode(mode))


class TestIsAutonomousDriving:
    """The autonomous-driving classifier is the load-bearing predicate of the
    D-0016 mode gate — empty prompt + meta-only is single-shot (allowed);
    anything else is autonomous (personal-only)."""

    def test_empty_prompt_no_commands_is_single_shot(self):
        from app.providers.cli_interactive import _is_autonomous_driving
        assert _is_autonomous_driving("", []) is False

    def test_empty_prompt_only_meta_commands_is_single_shot(self):
        from app.providers.cli_interactive import _is_autonomous_driving
        assert _is_autonomous_driving("", ["/usage"]) is False
        assert _is_autonomous_driving("", ["/model", "/usage"]) is False

    def test_grok_usage_show_head_matches_meta(self):
        # Tail args don't disqualify meta — only the head token is checked.
        from app.providers.cli_interactive import _is_autonomous_driving
        assert _is_autonomous_driving("", ["/usage show"]) is False

    def test_non_empty_prompt_is_autonomous(self):
        from app.providers.cli_interactive import _is_autonomous_driving
        assert _is_autonomous_driving("do the thing", []) is True

    def test_non_meta_control_command_is_autonomous(self):
        from app.providers.cli_interactive import _is_autonomous_driving
        assert _is_autonomous_driving("", ["/exec ls"]) is True
        assert _is_autonomous_driving("", ["/usage", "/exec ls"]) is True


class TestIsMetaCapture:
    """The read-only meta-capture predicate (D-0023 / A): empty prompt + at least
    one command, all of which are single-shot meta queries. These bypass the
    terminal-seam master switch. Per-provider commands all reduce to a meta head."""

    def test_meta_only_empty_prompt_is_capture(self):
        from app.providers.cli_interactive import _is_meta_capture
        assert _is_meta_capture("", ["/usage"]) is True          # claude / agy
        assert _is_meta_capture("", ["/status"]) is True         # codex
        assert _is_meta_capture("", ["/usage show"]) is True     # grok (head-matched)

    def test_no_commands_is_not_capture(self):
        from app.providers.cli_interactive import _is_meta_capture
        assert _is_meta_capture("", []) is False

    def test_prompt_present_is_not_capture(self):
        from app.providers.cli_interactive import _is_meta_capture
        assert _is_meta_capture("do the thing", ["/usage"]) is False

    def test_non_meta_command_is_not_capture(self):
        from app.providers.cli_interactive import _is_meta_capture
        assert _is_meta_capture("", ["/clear"]) is False
        assert _is_meta_capture("", ["/usage", "/exec ls"]) is False


class TestModeGate:
    """D-0016: autonomous full-TTY driving is personal-mode only; single-shot
    meta queries are allowed in every mode."""

    @pytest.mark.asyncio
    async def test_oss_refuses_autonomous_task_prompt(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"})),
        )
        _set_mode(monkeypatch, "oss")
        evs = await _collect(_executor(), prompt="research AI news")
        err = evs[-1]
        assert err.kind == EventKind.error
        assert "personal-mode only" in err.message
        assert err.data["mode"] == "oss"
        assert err.data["reason"] == "D-0016 autonomous driving"

    @pytest.mark.asyncio
    async def test_oss_refuses_non_meta_control_command(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(
                enabled=True,
                allowed_commands=frozenset({"/usage", "/exec"}),  # allow-policy
            ),
        )
        _set_mode(monkeypatch, "oss")
        # Allow-policy lets `/exec` through; mode gate must still refuse it,
        # since it isn't a single-shot meta command.
        evs = await _collect(
            _executor(), prompt="",
            extra={"control_commands": ["/exec ls"]},
        )
        err = evs[-1]
        assert err.kind == EventKind.error
        assert "personal-mode only" in err.message

    @pytest.mark.asyncio
    async def test_oss_allows_single_shot_meta(self, monkeypatch):
        # Empty prompt + meta-only must pass the mode gate. The seam will then
        # fail on missing binary (we don't actually spawn) — the assertion is
        # that the failure isn't the D-0016 refusal.
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"})),
        )
        _set_mode(monkeypatch, "oss")
        pdef = ProviderDef(name="x", kind="cli", tier="agent", cli_binary=None)
        ex = CLIInteractiveExecutor(pdef, instance=ProviderInstance(id="x", template="x", label="x"))
        evs = await _collect(ex, prompt="", extra={"control_commands": ["/usage"]})
        # Should fail on missing-binary (passed the mode gate) — not "personal-mode only".
        assert evs[-1].kind == EventKind.error
        assert "personal-mode only" not in evs[-1].message

    @pytest.mark.asyncio
    async def test_personal_allows_autonomous_driving(self, monkeypatch):
        # personal mode is the only place autonomous full-TTY driving lives;
        # the seam should reach the spawn path (and fail on missing binary
        # since we don't have a real CLI here).
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"})),
        )
        _set_mode(monkeypatch, "personal")
        pdef = ProviderDef(name="x", kind="cli", tier="agent", cli_binary=None)
        ex = CLIInteractiveExecutor(pdef, instance=ProviderInstance(id="x", template="x", label="x"))
        evs = await _collect(ex, prompt="task prompt that would drive autonomously")
        assert evs[-1].kind == EventKind.error
        # Reaches the missing-binary error, not the D-0016 refusal.
        assert "personal-mode only" not in evs[-1].message
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"})),
        )
        pdef = ProviderDef(name="x", kind="cli", tier="agent", cli_binary=None)
        ex = CLIInteractiveExecutor(pdef, instance=ProviderInstance(id="x", template="x", label="x"))
        evs = await _collect(ex)
        assert evs[-1].kind == EventKind.error
        assert "No CLI binary" in evs[-1].message
