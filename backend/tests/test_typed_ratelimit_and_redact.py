"""
tests/test_typed_ratelimit_and_redact.py — D-0058 slice 2 (A4 + A6).

A4 — typed rate-limit signals: codex `turn.failed`/`error` stdout events and
grok's `stopReason:"rate_limit"` end event must classify as rate limits at the
typed surface (stderr regex demoted to fallback), producing the same
`data={"rate_limit": True, "reset_at": …}` contract the orchestrator's
cooldown/failover path already consumes.

A6 — secrets wall: `redact_text`/`redact_json` catch known credential shapes
and keyed assignments without mangling benign text, and the JSON log formatter
applies the wall to every emitted line.
"""
from __future__ import annotations

import json
import logging

import pytest

from app import sandbox
from app.logging_config import JsonFormatter
from app.providers.base import EventKind
from app.providers.cli_executor import CLIExecutor, parse_line
from app.providers.registry import ProviderDef
from app.redact import REDACTED, redact_json, redact_text


# ── A4: codex typed failure events (parse_line) ───────────────────────────────

def test_codex_turn_failed_usage_limit_is_typed_rate_limit():
    line = json.dumps({
        "type": "turn.failed",
        "error": {"message": "You've hit your usage limit. Switch to another "
                             "model now, or try again at 3:45 PM."},
    })
    ev = parse_line(line, [])
    assert ev is not None
    assert ev.kind == EventKind.error
    assert ev.data["rate_limit"] is True
    assert ev.data["reset_at"]  # default cooldown at minimum
    assert ev.message.startswith("rate_limit_reached:")


def test_codex_error_event_out_of_credits_is_rate_limit():
    line = json.dumps({
        "type": "error",
        "message": "Your workspace is out of credits. Add credits to continue.",
    })
    ev = parse_line(line, [])
    assert ev.kind == EventKind.error
    assert ev.data["rate_limit"] is True


def test_codex_turn_failed_other_error_is_plain_error():
    line = json.dumps({
        "type": "turn.failed",
        "error": {"message": "internal error; agent loop died unexpectedly"},
    })
    ev = parse_line(line, [])
    assert ev.kind == EventKind.error
    assert "rate_limit" not in ev.data
    assert "agent loop died" in ev.message


def test_typed_error_events_are_terminal():
    ev = parse_line(json.dumps({"type": "error", "message": "boom"}), [])
    assert ev.is_terminal()


# ── A4: grok stopReason:"rate_limit" (run_stream) ─────────────────────────────

class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def _gen():
            for ln in self._lines:
                yield ln
        return _gen()


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes], stderr_lines: list[bytes]) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover
        self.returncode = -9


@pytest.mark.asyncio
async def test_grok_rate_limit_stop_reason_yields_typed_rate_limit(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: False)

    stdout_lines = [
        b'{"type":"thought","data":"Working"}\n',
        b'{"type":"end","stopReason":"rate_limit"}\n',
    ]

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(stdout_lines, [])

    monkeypatch.setattr(
        "app.providers.cli_executor.asyncio.create_subprocess_exec", _fake_exec
    )

    executor = CLIExecutor(ProviderDef(name="grok", kind="cli", tier="agent",
                                       cli_binary="grok"))
    events = [ev async for ev in executor.run_stream("task", workdir="/tmp")]

    errors = [ev for ev in events if ev.kind == EventKind.error]
    assert len(errors) == 1
    assert errors[0].data["rate_limit"] is True
    assert errors[0].data["reset_at"]
    assert not any(ev.kind == EventKind.result for ev in events)


# ── A6: redact_text / redact_json ─────────────────────────────────────────────

@pytest.mark.parametrize("secret", [
    "sk-proj-Abc123XyzAbc123XyzAbc123",
    "xai-Abc123XyzAbc123XyzAbc",
    "ghp_Abcdefghij0123456789Abcd",
    "github_pat_11ABCDEFG0_abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-123456789012-abcdefgh",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4",
    "gAAAAABkZ0x1c2VyX3NlY3JldF9wYXlsb2Fk",
])
def test_redact_text_catches_known_shapes(secret):
    out = redact_text(f"agent printed {secret} to stdout")
    assert secret not in out
    assert REDACTED in out


def test_redact_text_keyed_forms_keep_the_name():
    out = redact_text("Authorization: Bearer abc123def456\nOPENAI_API_KEY=supersecret99")
    assert "abc123def456" not in out
    assert "supersecret99" not in out
    assert "Authorization:" in out
    assert "OPENAI_API_KEY=" in out


def test_redact_text_leaves_benign_text_alone():
    benign = "max_tokens=4096 tokens=123456 usage limit reached; cost_usd=0.42"
    assert redact_text(benign) == benign


def test_redact_text_pem_block():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow…base64…\n"
           "-----END RSA PRIVATE KEY-----")
    out = redact_text(f"found key:\n{pem}\ndone")
    assert "MIIEow" not in out
    assert REDACTED in out


def test_redact_json_sensitive_keys_and_nested_strings():
    obj = {
        "api_key": "plainvalue",
        "nested": {"note": "token here: ghp_Abcdefghij0123456789Abcd"},
        "count": 42,
        "items": ["ok", "sk-proj-Abc123XyzAbc123XyzAbc123"],
    }
    out = redact_json(obj)
    assert out["api_key"] == REDACTED
    assert "ghp_" not in out["nested"]["note"]
    assert out["count"] == 42
    assert out["items"][0] == "ok"
    assert REDACTED in out["items"][1]


# ── A6: JSON log formatter applies the wall ───────────────────────────────────

def test_json_formatter_redacts_log_lines():
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="agent echoed OPENAI_API_KEY=%s", args=("sk-proj-Abc123XyzAbc123XyzAbc123",),
        exc_info=None,
    )
    line = JsonFormatter().format(record)
    assert "sk-proj" not in line
    assert REDACTED in line
    json.loads(line)  # still valid JSON
