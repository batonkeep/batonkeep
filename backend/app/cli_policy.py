"""
cli_policy.py — Terminal-seam command/exec allow-policy (D-0015 / P-0018).

The PTY interactive-CLI seam (app/providers/cli_interactive.py) drives full TUI
sessions, a much wider surface than headless `cli -p`. This module is the
configurable boundary that bounds it. Two facets:

  1. Control commands the seam may **send** into the TUI (slash commands such as
     `/usage`). Default-deny allowlist — only listed commands are permitted.
  2. Whether the driven CLI may **auto-run shell/tool** commands the model emits.
     When off, the seam launches the CLI without its skip-permission flag so
     model-generated shell stays gated.

Config (see app/config.py): terminal_seam_enabled, terminal_allowed_commands,
terminal_allow_shell, terminal_policy_path (optional JSON that extends the list).

The policy is loaded once from settings + the optional JSON file. Enforcement
lives at the seam: it calls check_command() before sending anything, and reads
allow_shell when building the launch command. Default posture is closed.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminalPolicy:
    enabled: bool = False
    allowed_commands: frozenset[str] = field(default_factory=frozenset)
    allow_shell: bool = False

    def check_command(self, command: str) -> tuple[bool, str]:
        """
        Decide whether the seam may send `command` into the TUI.

        Returns (allowed, reason). Default-deny: a command must (a) ride on an
        enabled seam and (b) match the allowlist by its first token (so
        "/usage" authorises "/usage" with no trailing args, but a different verb
        is refused). Never throws — the caller logs + refuses on a False.
        """
        if not self.enabled:
            return False, "terminal seam disabled (terminal_seam_enabled=false)"
        verb = (command or "").strip().split(maxsplit=1)
        if not verb:
            return False, "empty command"
        head = verb[0]
        if head not in self.allowed_commands:
            return False, f"command {head!r} not in terminal_allowed_commands"
        return True, "ok"


def _parse_commands(raw: str) -> set[str]:
    return {c.strip() for c in (raw or "").split(",") if c.strip()}


def load_policy() -> TerminalPolicy:
    """Build the policy from settings, optionally extended by a JSON file.

    JSON schema (all optional): {"allowed_commands": [...], "allow_shell": bool,
    "enabled": bool}. File values *extend* the allowlist and override the bools.
    A missing/unreadable file is ignored (settings stand alone).
    """
    s = get_settings()
    enabled = s.terminal_seam_enabled
    allowed = _parse_commands(s.terminal_allowed_commands)
    allow_shell = s.terminal_allow_shell

    path = s.terminal_policy_path
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            allowed |= {str(c).strip() for c in data.get("allowed_commands", []) if str(c).strip()}
            if "allow_shell" in data:
                allow_shell = bool(data["allow_shell"])
            if "enabled" in data:
                enabled = bool(data["enabled"])
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            logger.error("[cli_policy] failed to load terminal_policy_path=%s: %s", path, exc)

    return TerminalPolicy(
        enabled=enabled,
        allowed_commands=frozenset(allowed),
        allow_shell=allow_shell,
    )


# Loaded once at import; the seam reads this. Tests call load_policy() directly
# after patching settings to exercise specific postures.
_POLICY: TerminalPolicy | None = None


def get_policy() -> TerminalPolicy:
    global _POLICY
    if _POLICY is None:
        _POLICY = load_policy()
    return _POLICY


def reload_policy() -> TerminalPolicy:
    """Force a re-read (after a settings/file change). Returns the new policy."""
    global _POLICY
    _POLICY = load_policy()
    return _POLICY
