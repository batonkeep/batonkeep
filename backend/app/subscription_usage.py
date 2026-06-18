"""
subscription_usage.py — D-0049: full-TTY /usage scrape REMOVED.

The background poll and TTY-capture path are gone. Provider quota % is no longer
displayed in the UI. The reliable surface is per-provider API cost from Run.cost_usd
(D-0042/P-0047), which Batonkeep computes exactly and owns.

What remains:
  - _USAGE_COMMAND: the per-provider usage command, used by the frontend's
    "Check usage → Open terminal" path to pre-fill the command in the web-TTY
    terminal (the user runs it themselves and reads the raw output).
  - usage_command_for(instance_id) -> str: looked up by the new
    GET /api/providers/{id}/usage-command endpoint so the UI knows what to pre-fill.

The parse_usage_panel / capture_subscription_usage / poll_all_subscription_usage
functions have been removed. The test file test_subscription_usage.py is also
removed in this PR (its subject no longer exists).
"""
from __future__ import annotations

from app.providers.registry import get_instance, get_provider_def

# Per-provider interactive usage command. The user pastes/runs this in their own
# terminal session when they click "Check usage → Open terminal" in the UI.
_USAGE_COMMAND: dict[str, str] = {
    "claude": "/usage",
    "codex": "/status",
    "agy": "/usage",
    "grok": "/usage show",
}
_DEFAULT_USAGE_COMMAND = "/usage"


def usage_command_for(instance_id: str) -> str:
    """Return the /usage command string for a given provider instance.

    Used by the UI's 'Check usage → Open terminal' path to pre-fill the command
    in the web-TTY terminal. The user runs it themselves and reads raw output.
    """
    inst = get_instance(instance_id)
    pdef = get_provider_def(inst.template) if inst else None
    provider = pdef.name if pdef else instance_id
    return _USAGE_COMMAND.get(provider, _DEFAULT_USAGE_COMMAND)
