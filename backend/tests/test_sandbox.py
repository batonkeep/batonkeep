"""
test_sandbox.py — the privilege-drop fence (P-0022/D-0020) + fail-closed gate,
and the workspace jail layered on top of it (P-0072).

`wrap()`'s three original behaviours: prefix with the spawner when available,
no-op when the spawner is absent in dev/tests, and REFUSE (fail closed) when the
spawner is absent but REQUIRE_SANDBOX is set — so agent / API-path code can never
silently run as the control-plane user (the P-0046 non-sandbox bug).

The jail is a *second* fence with a different job. The privilege drop separates
agents from the control plane; it cannot separate agents from **each other**,
because every session's agent runs as the same uid on deliberately
group-co-writable workspaces. Landlock is what does that, and these tests lock
the policy around it — the ruleset itself is kernel-enforced and only provable in
a built container.
"""
from __future__ import annotations

import pytest

from app import sandbox

SPAWNER = "/usr/local/bin/sandbox-spawn"


@pytest.fixture(autouse=True)
def _reset_jail_probe(monkeypatch):
    """The probe result is process-cached; tests must not inherit each other's."""
    monkeypatch.setattr(sandbox, "_jail_supported", None)
    monkeypatch.setattr(sandbox, "_jail_warned", False)
    monkeypatch.setattr(sandbox._settings, "sandbox_spawn_path", SPAWNER)


# ── The privilege drop (unchanged behaviour) ──────────────────────────────────

def test_wrap_noop_when_unavailable_and_not_required(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: False)
    cmd = ["node", "build.js"]
    assert sandbox.wrap(cmd, jail="/data/sessions/s1") == cmd  # direct dev spawn


def test_wrap_prefixes_spawner_when_available(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: False)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "warn")
    assert sandbox.wrap(["echo", "hi"], jail=None) == [SPAWNER, "--", "echo", "hi"]


def test_wrap_fails_closed_when_required_but_unavailable(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    monkeypatch.setattr(sandbox, "required", lambda: True)
    with pytest.raises(sandbox.SandboxUnavailableError):
        sandbox.wrap(["npm", "install"], jail=None)


def test_required_reads_setting(monkeypatch):
    monkeypatch.setattr(sandbox._settings, "require_sandbox", True)
    assert sandbox.required() is True
    monkeypatch.setattr(sandbox._settings, "require_sandbox", False)
    assert sandbox.required() is False


# ── The workspace jail (P-0072) ───────────────────────────────────────────────

def test_jail_is_passed_to_the_spawner(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: True)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "warn")

    assert sandbox.wrap(["claude"], jail="/data/sessions/s1") == [
        SPAWNER, "--jail", "/data/sessions/s1", "--", "claude",
    ]


def test_jail_path_is_realpathed(monkeypatch, tmp_path):
    """Landlock resolves the real path, so a symlinked workdir would otherwise
    confine the agent to somewhere it never looks."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: True)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "warn")

    assert sandbox.wrap(["sh"], jail=str(link))[2] == str(real.resolve())


def test_warn_mode_degrades_when_the_kernel_cannot_enforce(monkeypatch, caplog):
    """An old kernel keeps working — but says so, once, at WARNING."""
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: False)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "warn")

    with caplog.at_level("WARNING"):
        first = sandbox.wrap(["claude"], jail="/data/sessions/s1")
        sandbox.wrap(["grok"], jail="/data/sessions/s2")

    assert first == [SPAWNER, "--", "claude"]
    assert sum("NO WORKSPACE JAIL" in r.message for r in caplog.records) == 1


def test_require_mode_refuses_when_the_kernel_cannot_enforce(monkeypatch):
    """`require` means require — an agent that could reach another session's
    workspace does not launch."""
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: False)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "require")

    with pytest.raises(sandbox.SandboxUnavailableError, match="Landlock"):
        sandbox.wrap(["claude"], jail="/data/sessions/s1")


def test_require_mode_refuses_a_launch_with_nothing_to_confine(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: True)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "require")

    with pytest.raises(sandbox.SandboxUnavailableError, match="no workspace"):
        sandbox.wrap(["some-mcp-server"], jail=None)


def test_off_mode_never_jails(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "jail_supported", lambda: True)
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "off")

    assert sandbox.wrap(["claude"], jail="/data/sessions/s1") == [SPAWNER, "--", "claude"]


def test_unknown_jail_mode_falls_back_to_warn(monkeypatch):
    """A typo must not silently disable the fence."""
    monkeypatch.setattr(sandbox._settings, "sandbox_jail", "yes-please")
    assert sandbox.jail_mode() == "warn"


def test_jail_support_is_probed_through_the_helper(monkeypatch):
    """Python does not guess kernel support — the helper is the process that must
    apply the ruleset, and it is built with or without Landlock at image time."""
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0

    def _run(cmd, **kw):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox.subprocess, "run", _run)

    assert sandbox.jail_supported() is True
    assert sandbox.jail_supported() is True  # cached, not re-probed
    assert calls == [[SPAWNER, "--jail-probe"]]


def test_probe_failure_is_treated_as_unsupported(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("no such binary")

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox.subprocess, "run", _boom)

    assert sandbox.jail_supported() is False


def test_no_spawner_means_no_jail_support(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    assert sandbox.jail_supported() is False
