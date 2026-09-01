"""P-0100 / Gate B4 — the approval webhook must never be able to break a run."""
from __future__ import annotations

import asyncio


def _call(**kw):
    from app import notify
    return asyncio.get_event_loop().run_until_complete(
        notify.approval_pending(
            request_id=kw.get("request_id", "r1"),
            run_id=kw.get("run_id", 7),
            label=kw.get("label"),
            producer=kw.get("producer", "mock"),
        )
    )


def test_unconfigured_is_a_silent_no_op(monkeypatch):
    """The default must cost nothing — no client, no DNS, no log noise."""
    from app import notify

    monkeypatch.setattr(notify._settings, "approval_webhook_url", "")

    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("no HTTP client should be constructed when unconfigured")

    monkeypatch.setattr(notify.httpx, "AsyncClient", _explode)
    assert _call() is None


def test_a_failing_webhook_never_raises(monkeypatch):
    """The whole contract: a down chat server must not wedge a parked run.

    Losing a notification is a missed ping. Raising here would propagate into the
    approver and change what the agent does — a far worse bug.
    """
    from app import notify

    monkeypatch.setattr(notify._settings, "approval_webhook_url", "http://127.0.0.1:1/hook")

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **k: _Boom())
    assert _call() is None


def test_payload_carries_context_but_not_the_code(monkeypatch):
    """A notification hop is the wrong place for the proposal body.

    The queue shows what is actually being approved; the ping only says where to look.
    """
    from app import notify

    monkeypatch.setattr(notify._settings, "approval_webhook_url", "http://example.invalid/h")
    sent: dict = {}

    class _Capture:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **k):
            sent["url"] = url
            sent["json"] = json

    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **k: _Capture())
    _call(run_id=42, label="install deps", producer="agy")

    assert sent["url"] == "http://example.invalid/h"
    body = sent["json"]
    assert body["event"] == "approval.pending"
    assert body["run_id"] == 42
    assert body["label"] == "install deps"
    assert "42" in body["text"]
    assert "code" not in body
