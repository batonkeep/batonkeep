"""
console.py — Scoped in-UI console actions (set models, run auth).

Security model (see Settings.web_console_available):
  - Off unless ENABLE_WEB_CONSOLE=true AND WEB_CONSOLE_TOKEN is set.
  - Never available in DEPLOYMENT_MODE=managed.
  - Every console request must present the token.

This is NOT a general shell. The only process it spawns is the project's own
auth.sh, with a single validated provider/instance argument — so it cannot run
arbitrary commands. It runs that login flow under a PTY and streams its output
over a WebSocket, forwarding the user's keystrokes to that one process so
interactive logins (device codes, redirect URLs) can be completed from the UI.
"""
from __future__ import annotations

import logging
import os

from app.config import get_settings
from app.pty_session import PtySession

logger = logging.getLogger(__name__)
_settings = get_settings()

AUTH_SCRIPT = "/app/scripts/auth.sh"


def valid_auth_target(target: str) -> bool:
    """A target is a known template or a declared instance id — nothing else."""
    from app.providers.registry import ALL_TEMPLATE_NAMES, get_instance

    template = target.split(":", 1)[0]
    if template not in ALL_TEMPLATE_NAMES:
        return False
    if ":" in target:
        return get_instance(target) is not None
    return True


class PtyAuthSession(PtySession):
    """Runs `auth.sh <target>` under a PTY, bridging it to a WebSocket.

    Thin wrapper over PtySession: the only command this lane ever spawns is the
    project's own auth.sh with one validated target — never an arbitrary command.
    """

    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(
            ["bash", AUTH_SCRIPT, target],
            env=os.environ.copy(),
        )
