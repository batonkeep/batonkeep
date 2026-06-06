"""
web_tty.py — Human-driven web-TTY provider sessions (D-0016 seam #3 / D-0017).

This is the *interactive studio* lane: a real provider CLI (claude / grok / agy /
codex) launched in a session's own workspace, streamed to the browser over a PTY,
with the user typing each turn. It is deliberately NOT the autonomous run_stream
loop (CLIInteractiveExecutor) — no prompt is injected, no control commands are
auto-sent. A human drives every keystroke, which is what keeps it on the right
side of the consumer-plan ToS posture (D-0016): Batonkeep is a terminal multiplexer
here, not a bot driving the CLI.

Locality (D-0017): the session runs in the user-owned workspace (sessions_dir);
nothing about the session is persisted Batonkeep-side beyond the workspace itself.

Gating lives at the route: the web console must be enabled + token-presented (so
never in managed, per Settings.web_console_available) and plan-CLI must be allowed
for this deployment. This module only resolves the target and builds the session.
"""
from __future__ import annotations

import logging
import os

from app.config import get_settings
from app.providers.registry import get_instance, get_provider_def
from app.pty_session import PtySession
from app.sessions.workspace import workspace_root

logger = logging.getLogger(__name__)
_settings = get_settings()


class WebTtyError(ValueError):
    """Raised when a web-TTY target can't be resolved into a launchable session."""


def build_web_tty_session(session_id: str, instance_id: str) -> PtySession:
    """Resolve (session × provider instance) into a launchable PtySession.

    Spawns the provider's interactive TUI (no -p, no skip-permission flag — the
    human approves tool use inside the CLI) with the instance's config dir, cwd'd
    into the session workspace. Raises WebTtyError with a user-facing reason.
    """
    if not _settings.plan_cli_allowed:
        raise WebTtyError(
            f"plan-CLI is not available in DEPLOYMENT_MODE={_settings.deployment_mode.value}"
        )

    instance = get_instance(instance_id)
    if instance is None:
        raise WebTtyError(f"unknown provider instance: {instance_id}")

    pdef = get_provider_def(instance.template)
    if pdef is None or pdef.kind != "cli" or not pdef.cli_binary:
        raise WebTtyError(f"provider {instance.template} has no interactive CLI")

    try:
        workspace = workspace_root(session_id)
    except ValueError as exc:
        raise WebTtyError(str(exc)) from exc
    if not os.path.isdir(workspace):
        raise WebTtyError(f"session workspace not found: {session_id}")

    env = os.environ.copy()
    # Stable terminal so the TUI emits a predictable escape vocabulary in xterm.js.
    env["TERM"] = "xterm-256color"
    # Point the CLI at this instance's own auth/config dir (per-account isolation),
    # exactly as the headless and single-shot seams do. Never touches OAuth tokens.
    if instance.cli_config_dir and instance.cli_config_env:
        env[instance.cli_config_env] = instance.cli_config_dir

    argv = [pdef.cli_binary]
    logger.info(
        "web-tty session: %s in workspace %s (%s)",
        instance.id, session_id, pdef.cli_binary,
    )
    return PtySession(argv, cwd=workspace, env=env)
