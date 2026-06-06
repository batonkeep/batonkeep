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


async def _collect(executor, **kw):
    return [ev async for ev in executor.run_stream("hi", workdir="/tmp", **kw)]


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
    async def test_disabled_seam_refuses_before_spawn(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.cli_interactive.get_policy",
            lambda: TerminalPolicy(enabled=False, allowed_commands=frozenset({"/usage"})),
        )
        evs = await _collect(_executor(), extra={"control_commands": ["/usage"]})
        assert evs[-1].kind == EventKind.error
        assert "disabled" in evs[-1].message

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
