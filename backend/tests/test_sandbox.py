"""
test_sandbox.py — the privilege-drop fence (P-0022/D-0020) + fail-closed gate.

Locks `wrap()`'s three behaviours: prefix with the spawner when available, no-op
when the spawner is absent in dev/tests, and REFUSE (fail closed) when the spawner
is absent but REQUIRE_SANDBOX is set — so agent / API-path code can never silently
run as the control-plane user (the P-0046 non-sandbox bug).
"""
from __future__ import annotations

import pytest

from app import sandbox


def test_wrap_noop_when_unavailable_and_not_required(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: False)
    cmd = ["node", "build.js"]
    assert sandbox.wrap(cmd) == cmd  # unchanged → direct dev spawn


def test_wrap_prefixes_spawner_when_available(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox._settings, "sandbox_spawn_path", "/usr/local/bin/sandbox-spawn")
    assert sandbox.wrap(["echo", "hi"]) == ["/usr/local/bin/sandbox-spawn", "--", "echo", "hi"]


def test_wrap_fails_closed_when_required_but_unavailable(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: True)
    with pytest.raises(sandbox.SandboxUnavailableError):
        sandbox.wrap(["npm", "install"])


def test_required_reads_setting(monkeypatch):
    monkeypatch.setattr(sandbox._settings, "require_sandbox", True)
    assert sandbox.required() is True
    monkeypatch.setattr(sandbox._settings, "require_sandbox", False)
    assert sandbox.required() is False
