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
    def test_extracts_highest_percentage(self):
        text = "Current session 12% used\nCurrent week (all models) 67% used"
        u = parse_usage_panel(text, "claude")
        assert u.ok is True
        assert u.used_pct == 0.67

    def test_extracts_reset_hint(self):
        text = "Weekly limit 80% used\nResets Mon Jun 9 at 10:00"
        u = parse_usage_panel(text, "claude")
        assert u.reset_hint and u.reset_hint.startswith("Mon Jun 9")

    def test_no_percentage_is_not_ok(self):
        u = parse_usage_panel("Welcome to the CLI. Type /help.", "claude")
        assert u.ok is False
        assert "no percentage" in (u.error or "")

    def test_empty_panel(self):
        u = parse_usage_panel("", "claude")
        assert u.ok is False
        assert u.error == "empty panel"

    def test_ignores_out_of_range_numbers(self):
        # 200% is not a valid usage fraction; 45% is.
        u = parse_usage_panel("port 8000, 45% used, 200%", "claude")
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
    monkeypatch.setattr("app.subscription_usage.get_executor", lambda _id: _FakeExecutor(events))
    u = await capture_subscription_usage("claude")
    assert u.ok is True
    assert u.used_pct == 0.55
    assert quota_tracker.get_health("claude").est_used_pct == 0.55


@pytest.mark.asyncio
async def test_capture_unknown_instance(monkeypatch):
    monkeypatch.setattr("app.subscription_usage.get_executor", lambda _id: None)
    u = await capture_subscription_usage("nope")
    assert u.ok is False
    assert "unavailable" in (u.error or "")


@pytest.mark.asyncio
async def test_capture_surfaces_seam_error(monkeypatch):
    events = [ExecEvent(kind=EventKind.error, message="terminal seam disabled")]
    monkeypatch.setattr("app.subscription_usage.get_executor", lambda _id: _FakeExecutor(events))
    u = await capture_subscription_usage("claude")
    assert u.ok is False
    assert "disabled" in (u.error or "")
