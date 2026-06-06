"""
test_subscription_usage.py — D-0015 #4: plan-CLI /usage capture + parse.

Parsing is the pure core; capture is exercised with a fake executor so no real
TUI is spawned. A usable reading must flow into the quota tracker.
"""
from __future__ import annotations

import pytest

from app.providers.base import EventKind, ExecEvent, ExecResult, Usage
from app.quota import quota_tracker
from app.subscription_usage import (
    SubscriptionUsage,
    capture_subscription_usage,
    parse_usage_panel,
)


class TestParseUsagePanel:
    def test_claude_used_framing_takes_highest(self):
        # claude frames as "% used"; the binding constraint is the highest.
        text = "Current session 12% used\nCurrent week (all models) 67% used"
        u = parse_usage_panel(text, "claude")
        assert u.ok is True
        assert u.used_pct == 0.67

    def test_codex_left_framing_is_inverted(self):
        # codex frames as "% left" → used = 100 - left.
        u = parse_usage_panel("Monthly limit: 87% left (resets 13:53 on 2 Jul)", "codex")
        assert u.ok is True
        assert u.used_pct == pytest.approx(0.13)

    def test_agy_available_framing_is_inverted(self):
        # agy frames as "% available" per model → 100% available = 0% used.
        u = parse_usage_panel("Gemini 3.5 Flash\n100% Quota available", "agy")
        assert u.ok is True
        assert u.used_pct == 0.0

    def test_grok_credits_percentage(self):
        # grok's live panel (2026-06-06): "Credits used: NN%" — framing-word path.
        u = parse_usage_panel("Credits used: 9%", "grok")
        assert u.ok is True
        assert u.used_pct == pytest.approx(0.09)

    def test_grok_credits_count_fallback(self):
        # Defensive: count/total variant → used = count / total.
        u = parse_usage_panel("Credits used: 2,500 / 10,000", "grok")
        assert u.ok is True
        assert u.used_pct == pytest.approx(0.25)

    def test_grok_credits_remaining_framing_is_inverted(self):
        # "remaining" counts the unused credits → used = (total - count) / total.
        u = parse_usage_panel("Credits remaining: 7,500 of 10,000", "grok")
        assert u.ok is True
        assert u.used_pct == pytest.approx(0.25)

    def test_extracts_reset_hint(self):
        text = "Weekly limit 80% used\nResets Mon Jun 9 at 10:00"
        u = parse_usage_panel(text, "claude")
        assert u.reset_hint and u.reset_hint.startswith("Mon Jun 9")

    def test_reset_hint_trims_box_glyphs(self):
        u = parse_usage_panel("87% left (resets 13:53 on 2 Jul) │", "codex")
        assert u.reset_hint == "13:53 on 2 Jul)"

    def test_bare_percentage_without_framing_is_ignored(self):
        # No "used"/"left"/"available" near the % → not a quota gauge.
        u = parse_usage_panel("Loaded in 100% of cases. Welcome!", "claude")
        assert u.ok is False
        assert "no quota percentage" in (u.error or "")

    def test_no_percentage_is_not_ok(self):
        u = parse_usage_panel("Welcome to the CLI. Type /help.", "claude")
        assert u.ok is False

    def test_empty_panel(self):
        u = parse_usage_panel("", "claude")
        assert u.ok is False
        assert u.error == "empty panel"

    def test_ignores_out_of_range_and_unframed_numbers(self):
        # "200%" is out of range; "8000" has no %; only "45% used" is a real gauge.
        u = parse_usage_panel("port 8000, 45% used, 200% used", "claude")
        assert u.used_pct == 0.45


class _FakeExecutor:
    def __init__(self, events):
        self._events = events

    async def run_stream(self, prompt, **kw):
        for ev in self._events:
            yield ev


@pytest.mark.asyncio
async def test_capture_feeds_quota_tracker(monkeypatch):
    events = [
        ExecEvent(kind=EventKind.token, text="Current week 55% used\nResets Tue"),
        ExecEvent(kind=EventKind.result, text="", data={"result": ExecResult("", Usage(), "claude", "claude")}),
    ]
    monkeypatch.setattr("app.subscription_usage.get_interactive_executor", lambda _id: _FakeExecutor(events))
    u = await capture_subscription_usage("claude")
    assert u.ok is True
    assert u.used_pct == 0.55
    assert quota_tracker.get_health("claude").est_used_pct == 0.55


@pytest.mark.asyncio
async def test_capture_unknown_instance(monkeypatch):
    monkeypatch.setattr("app.subscription_usage.get_interactive_executor", lambda _id: None)
    u = await capture_subscription_usage("nope")
    assert u.ok is False
    assert "no interactive CLI" in (u.error or "")


@pytest.mark.asyncio
async def test_capture_surfaces_seam_error(monkeypatch):
    events = [ExecEvent(kind=EventKind.error, message="terminal seam disabled")]
    monkeypatch.setattr("app.subscription_usage.get_interactive_executor", lambda _id: _FakeExecutor(events))
    u = await capture_subscription_usage("claude")
    assert u.ok is False
    assert "disabled" in (u.error or "")
