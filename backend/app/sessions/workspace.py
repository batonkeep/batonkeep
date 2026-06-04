"""
sessions/workspace.py — sandboxed, git-init'd per-session workspaces (M1.1).

Filesystem-as-context (D-0008): the workspace directory is the source of truth.
Each session gets an isolated subdirectory under SESSIONS_DIR; the agent edits
files there, and a rolling SESSION.md brief captures goal + decisions so a
switched-in agent can continue without the prior agent's context window.

Isolation (sandbox-isolation skill, M1.1 level): simple directory isolation —
one workspace dir per session, with path-traversal-safe resolution so a session
can never read/write outside its own root. Container-grade isolation graduates
in M1.2+.

Git: the workspace is git-init'd at creation with an initial commit. Per-turn
auto-commit + diff/rollback land in M1.3; M1.1 only establishes the repo.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

BRIEF_FILENAME = "SESSION.md"

_BRIEF_TEMPLATE = """\
# Session brief

> Agent-prepared working brief (D-0008). The orchestrator may review/modify it.
> A switched-in agent reads this + the workspace files to continue — it does NOT
> receive the prior agent's chat transcript.

- **Title:** {title}
- **Goal:** {goal}

## Decisions / progress
_(none yet)_
"""


def workspace_root(session_id: str) -> str:
    """Absolute path to a session's workspace root. session_id must be a bare token."""
    if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return os.path.abspath(os.path.join(_settings.sessions_dir, session_id))


def safe_join(workspace: str, relpath: str) -> str:
    """
    Resolve relpath inside the workspace, refusing any path that escapes it
    (path traversal / absolute paths). Returns the absolute path.
    """
    root = os.path.abspath(workspace)
    candidate = os.path.abspath(os.path.join(root, relpath))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(f"path escapes workspace: {relpath!r}")
    return candidate


async def _git(workspace: str, *args: str) -> None:
    """Run a git command in the workspace; log (don't raise) on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", workspace, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("[workspace] git %s failed: %s", args, err.decode("utf-8", "replace").strip())


async def create_workspace(session_id: str, *, title: str, goal: str) -> str:
    """
    Create the sandboxed workspace dir, seed SESSION.md, git-init with an initial
    commit. Returns the absolute workspace path. Idempotent-ish: re-creating an
    existing dir is tolerated.
    """
    root = workspace_root(session_id)
    os.makedirs(root, exist_ok=True)

    brief_path = os.path.join(root, BRIEF_FILENAME)
    if not os.path.exists(brief_path):
        with open(brief_path, "w", encoding="utf-8") as f:
            f.write(_BRIEF_TEMPLATE.format(title=title, goal=goal or "_(not yet stated)_"))

    if not os.path.isdir(os.path.join(root, ".git")):
        await _git(root, "init", "-q")
        # Local identity so commits work without global git config.
        await _git(root, "config", "user.email", "agent@batonkeep.local")
        await _git(root, "config", "user.name", "batonkeep")
        await _git(root, "add", "-A")
        await _git(root, "commit", "-q", "-m", "session: initialise workspace")
    return root


def read_brief(workspace: str) -> str:
    """Return the SESSION.md brief text, or '' if absent."""
    path = os.path.join(workspace, BRIEF_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def append_progress(workspace: str, note: str) -> None:
    """
    Append a progress note to SESSION.md. In M1.1 the orchestrator maintains the
    brief on the agent's behalf (agent-prepared/orchestrator-reviewed per D-0008);
    real agents will write it themselves as the model matures.
    """
    path = os.path.join(workspace, BRIEF_FILENAME)
    line = f"- {note.strip()}\n"
    try:
        existing = read_brief(workspace)
        # Drop the placeholder once real progress arrives.
        if "_(none yet)_" in existing:
            existing = existing.replace("_(none yet)_\n", "").replace("_(none yet)_", "")
            with open(path, "w", encoding="utf-8") as f:
                f.write(existing.rstrip() + "\n" + line)
        else:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as exc:
        logger.warning("[workspace] could not update brief: %s", exc)


def list_files(workspace: str) -> list[str]:
    """Relative paths of files in the workspace, excluding the .git internals."""
    out: list[str] = []
    root = os.path.abspath(workspace)
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel)
    return sorted(out)


def build_turn_context(workspace: str, user_message: str) -> str:
    """
    Assemble the prompt for a turn from the *workspace*, not a replayed transcript
    (D-0008). This is what lets a switched-in agent continue seamlessly: it sees
    the SESSION.md brief + the current file list + the new user message.
    """
    brief = read_brief(workspace)
    files = list_files(workspace)
    file_list = "\n".join(f"- {f}" for f in files) if files else "- (empty)"
    return (
        f"{brief}\n\n"
        f"## Workspace files\n{file_list}\n\n"
        f"## User message\n{user_message}\n"
    )
