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

Git: the workspace is git-init'd at creation with an initial commit. The
orchestrator auto-commits the workspace per turn (M1.3) — the engine owns the
commit boundary, so versioning works for any executor. Each commit is a
**version**; per-turn diffs feed the live event view and `restore_version` does
a checkout-restore (shown to non-coders as Undo/History, never "git").
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
    await _git_out(workspace, *args)


async def _git_out(workspace: str, *args: str) -> tuple[int, str]:
    """
    Run a git command and capture stdout. Returns (returncode, stdout). Logs (does
    not raise) on non-zero exit so the session loop is never broken by a git
    hiccup — versioning is best-effort over the turn lifecycle.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", workspace, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("[workspace] git %s failed: %s", args, err.decode("utf-8", "replace").strip())
    return proc.returncode, out.decode("utf-8", "replace")


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


# ── Versioning: per-turn commits, diff, restore (M1.3) ────────────────────────
#
# The orchestrator owns the commit boundary (D-0008): after each turn it calls
# commit_turn(), so every turn that changed files becomes one git commit — a
# "version". This works for any executor (the engine commits, not the agent).
# Surfaced to non-coders as Undo/History; "git" is never shown.

# Hard cap on diff text returned to the UI so a huge generated asset can't bloat
# an event payload / DB row. Truncated diffs still show what changed.
_MAX_DIFF_CHARS = 20_000


def _trunc(text: str) -> str:
    if len(text) > _MAX_DIFF_CHARS:
        return text[:_MAX_DIFF_CHARS] + "\n… (diff truncated)\n"
    return text


async def commit_turn(
    workspace: str, *, seq: int, provider: str, summary: str = ""
) -> Optional[dict]:
    """
    Stage and commit the workspace after a turn. Returns a version dict
    {commit, short, message, diffstat, diff} for the commit just made, or None if
    the turn changed nothing (no commit created). Best-effort: never raises.
    """
    await _git(workspace, "add", "-A")
    # Nothing staged → don't create an empty commit (keeps history meaningful).
    code, _ = await _git_out(workspace, "diff", "--cached", "--quiet")
    if code == 0:
        return None

    headline = (summary.splitlines() or [""])[0][:72] if summary else ""
    message = f"turn {seq} ({provider})" + (f": {headline}" if headline else "")
    await _git(workspace, "commit", "-q", "-m", message)

    _, sha = await _git_out(workspace, "rev-parse", "HEAD")
    sha = sha.strip()
    if not sha:
        return None
    _, short = await _git_out(workspace, "rev-parse", "--short", "HEAD")
    diffstat = await _commit_diffstat(workspace, sha)
    diff = await _commit_diff(workspace, sha)
    return {
        "commit": sha,
        "short": short.strip(),
        "message": message,
        "diffstat": diffstat,
        "diff": diff,
    }


async def _commit_diff(workspace: str, sha: str) -> str:
    """Unified diff introduced by `sha` (vs its parent; full patch for a root commit)."""
    _, out = await _git_out(
        workspace, "show", "--no-color", "--format=", "--patch", sha
    )
    return _trunc(out.strip())


async def _commit_diffstat(workspace: str, sha: str) -> str:
    _, out = await _git_out(
        workspace, "show", "--no-color", "--format=", "--stat", sha
    )
    return out.strip()


async def list_versions(workspace: str) -> list[dict]:
    """
    All workspace versions (commits), newest first. Each entry:
    {commit, short, message, ts} — the Undo/History list for the UI.
    """
    _, out = await _git_out(
        workspace, "log", "--no-color", "--pretty=format:%H%x1f%h%x1f%cI%x1f%s"
    )
    versions: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        full, short, ts, subject = parts
        versions.append({"commit": full, "short": short, "ts": ts, "message": subject})
    return versions


async def version_diff(workspace: str, commit: str) -> Optional[dict]:
    """Diff + diffstat introduced by a specific version, or None if unknown."""
    if not _is_valid_sha(commit):
        return None
    code, _ = await _git_out(workspace, "cat-file", "-e", f"{commit}^{{commit}}")
    if code != 0:
        return None
    return {
        "commit": commit,
        "diffstat": await _commit_diffstat(workspace, commit),
        "diff": await _commit_diff(workspace, commit),
    }


async def restore_version(workspace: str, commit: str) -> Optional[dict]:
    """
    Restore the workspace to an earlier version (Undo/History). Implemented as a
    checkout-restore: the tree of `commit` is checked out over the working tree and
    committed as a NEW version, so nothing in history is lost and the restore is
    itself undoable. Returns the new version dict, or None on failure / no-op.
    """
    if not _is_valid_sha(commit):
        return None
    code, _ = await _git_out(workspace, "cat-file", "-e", f"{commit}^{{commit}}")
    if code != 0:
        return None
    # Make the index match the target tree, write it to the worktree, then drop any
    # files that exist now but not in the target (added after it). HEAD is left
    # where it is, so the restore lands as a NEW commit and history is preserved.
    code, _ = await _git_out(workspace, "read-tree", commit)
    if code != 0:
        return None
    await _git(workspace, "checkout-index", "-f", "-a")
    await _git_out(workspace, "clean", "-fdq")
    await _git(workspace, "add", "-A")
    code, _ = await _git_out(workspace, "diff", "--cached", "--quiet")
    if code == 0:
        return None  # workspace already matched the target — no-op
    _, short = await _git_out(workspace, "rev-parse", "--short", commit)
    message = f"restore to {short.strip()}"
    await _git(workspace, "commit", "-q", "-m", message)
    _, sha = await _git_out(workspace, "rev-parse", "HEAD")
    return {"commit": sha.strip(), "message": message, "restored_from": commit}


def _is_valid_sha(commit: str) -> bool:
    """A commit ref must be a bare hex sha (full or abbreviated) — no refspecs."""
    return bool(commit) and len(commit) <= 40 and all(c in "0123456789abcdef" for c in commit.lower())
