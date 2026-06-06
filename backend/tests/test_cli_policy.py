"""
test_cli_policy.py — D-0015 terminal-seam allow-policy.

Default-deny: the seam refuses unless explicitly enabled AND the command is on
the configured allowlist. allow_shell gates auto-run of model shell.
"""
from __future__ import annotations

import json

import pytest

from app.cli_policy import TerminalPolicy, load_policy, reload_policy


class TestCheckCommand:
    def test_disabled_seam_refuses_everything(self):
        p = TerminalPolicy(enabled=False, allowed_commands=frozenset({"/usage"}))
        ok, reason = p.check_command("/usage")
        assert ok is False
        assert "disabled" in reason

    def test_allowed_command_passes(self):
        p = TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage", "/status"}))
        assert p.check_command("/usage")[0] is True
        assert p.check_command("/status")[0] is True

    def test_command_not_in_allowlist_refused(self):
        p = TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"}))
        ok, reason = p.check_command("/exec rm -rf /")
        assert ok is False
        assert "not in" in reason

    def test_allowlist_matches_first_token_only(self):
        """'/usage' authorises the verb; args don't smuggle a different command."""
        p = TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"}))
        assert p.check_command("/usage")[0] is True
        # a different verb is refused even if it contains an allowed substring
        assert p.check_command("/usagехack")[0] is False

    def test_empty_command_refused(self):
        p = TerminalPolicy(enabled=True, allowed_commands=frozenset({"/usage"}))
        assert p.check_command("   ")[0] is False


class TestLoadPolicy:
    def test_defaults_are_closed(self, monkeypatch):
        from app.config import get_settings
        s = get_settings()
        for k, v in {
            "terminal_seam_enabled": False,
            "terminal_allowed_commands": "/usage,/status,/cost",
            "terminal_allow_shell": False,
            "terminal_policy_path": "",
        }.items():
            s.__dict__[k] = v
        p = reload_policy()
        assert p.enabled is False
        assert p.allow_shell is False
        assert "/usage" in p.allowed_commands

    def test_json_file_extends_allowlist_and_overrides_bools(self, tmp_path, monkeypatch):
        from app.config import get_settings
        pol = tmp_path / "policy.json"
        pol.write_text(json.dumps({
            "allowed_commands": ["/model"],
            "allow_shell": True,
            "enabled": True,
        }))
        s = get_settings()
        s.__dict__["terminal_seam_enabled"] = False  # file flips it on
        s.__dict__["terminal_allowed_commands"] = "/usage"
        s.__dict__["terminal_allow_shell"] = False
        s.__dict__["terminal_policy_path"] = str(pol)
        try:
            p = load_policy()
            assert p.enabled is True
            assert p.allow_shell is True
            assert {"/usage", "/model"} <= p.allowed_commands
        finally:
            s.__dict__["terminal_policy_path"] = ""
            reload_policy()
