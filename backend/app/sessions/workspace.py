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

# Markers for the auto-maintained summary block (D-0017 thread 1). Everything
# between them is owned by the summarizer (deterministic placeholder until an LLM
# summary runs); content outside is left untouched.
SUMMARY_BEGIN = "<!-- BATONKEEP:SUMMARY -->"
SUMMARY_END = "<!-- /BATONKEEP:SUMMARY -->"
ACTIVITY_HEADER = "## Activity"
_SUMMARY_PLACEHOLDER = "_(auto-maintained summary appears here once summarization runs)_"

_BRIEF_TEMPLATE = """\
# Session brief

> Agent-prepared working brief (D-0008). The orchestrator may review/modify it.
> A switched-in agent reads this + the workspace files to continue — it does NOT
> receive the prior agent's chat transcript.

- **Title:** {title}
- **Goal:** {goal}
{guidance}
## Summary
{summary_begin}
{summary_placeholder}
{summary_end}

## Activity
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


async def create_workspace(
    session_id: str, *, title: str, goal: str, guidance: str = ""
) -> str:
    """
    Create the sandboxed workspace dir, seed SESSION.md, git-init with an initial
    commit. Returns the absolute workspace path. Idempotent-ish: re-creating an
    existing dir is tolerated.

    `guidance` (optional) is a task-type block (D-0011) embedded into SESSION.md so
    the agent reads it on every turn — this is how session templates work without
    any orchestrator change.
    """
    root = workspace_root(session_id)
    # Group-write umask so the agents-group setgid dir stays co-writable by both
    # batond (git/commit/restore here) and the sandbox-user agent (file edits) —
    # P-0022/D-0020. The parent /data/sessions is setgid `agents`, so new subdirs
    # inherit the group; setgid + 0770 on the workspace root preserves group-write.
    prev_umask = os.umask(0o002)
    try:
        os.makedirs(root, exist_ok=True)
        try:
            os.chmod(root, 0o2770)  # setgid + group rwx
        except OSError as exc:  # best-effort: local/dev may not have the group
            logger.debug("[workspace] chmod 2770 %s skipped: %s", root, exc)
    finally:
        os.umask(prev_umask)

    guidance_block = f"\n## Task guidance\n{guidance.strip()}\n" if guidance.strip() else ""
    brief_path = os.path.join(root, BRIEF_FILENAME)
    if not os.path.exists(brief_path):
        with open(brief_path, "w", encoding="utf-8") as f:
            f.write(_BRIEF_TEMPLATE.format(
                title=title, goal=goal or "_(not yet stated)_", guidance=guidance_block,
                summary_begin=SUMMARY_BEGIN, summary_end=SUMMARY_END,
                summary_placeholder=_SUMMARY_PLACEHOLDER,
            ))

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


def _format_files(files: Optional[list[dict]]) -> str:
    """Compact one-line render of a turn's changed files (D-0017 thread 2 artifacts),
    grounded in git — the anti-drift anchor for the ledger."""
    if not files:
        return "(no file changes)"
    parts = []
    for f in files[:8]:
        adds, dels = f.get("additions"), f.get("deletions")
        delta = ""
        if adds is not None or dels is not None:
            bits = []
            if adds:
                bits.append(f"+{adds}")
            if dels:
                bits.append(f"−{dels}")
            delta = f" ({' '.join(bits)})" if bits else ""
        parts.append(f"{f['path']}{delta}")
    if len(files) > 8:
        parts.append(f"…+{len(files) - 8} more")
    return ", ".join(parts)


def record_turn(
    workspace: str,
    *,
    seq: int,
    provider: str,
    summary: str,
    files: Optional[list[dict]] = None,
    lane: str = "chat",
) -> None:
    """
    Append a structured entry to the ledger's Activity log (D-0017 thread 1).

    Each entry records the turn's provider/lane, a one-line summary, and the files
    it changed (the thread-2 artifacts, grounded in git so the ledger can't drift
    from what's actually on disk). This replaces the v0 flat note-log; the rich
    `## Summary` section above it is maintained separately by the summarizer.
    The orchestrator owns this on the agent's behalf (D-0008).
    """
    headline = (summary.strip().splitlines() or [""])[0][:140] if summary else ""
    headline = headline or "_(no text response)_"
    entry = f"- **turn {seq}** · {provider} · {lane} — {headline} — changed: {_format_files(files)}\n"

    path = os.path.join(workspace, BRIEF_FILENAME)
    try:
        existing = read_brief(workspace)
        if ACTIVITY_HEADER in existing:
            head, _, tail = existing.partition(ACTIVITY_HEADER)
            # Drop the "(none yet)" placeholder on the first real entry.
            tail = tail.replace("_(none yet)_\n", "").replace("_(none yet)_", "")
            new = f"{head}{ACTIVITY_HEADER}{tail.rstrip()}\n{entry}"
        else:
            # Older ledger without the section (or a hand-edited brief): append one.
            new = existing.rstrip() + f"\n\n{ACTIVITY_HEADER}\n{entry}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as exc:
        logger.warning("[workspace] could not update ledger: %s", exc)


def set_summary(workspace: str, text: str) -> None:
    """
    Replace the auto-maintained `## Summary` block (D-0017 thread 1). Upserts the
    managed block between SUMMARY markers, preserving everything else. If the brief
    predates the markers, inserts a `## Summary` section before `## Activity`
    (or at the end). Best-effort — never raises.
    """
    body = (text.strip() or _SUMMARY_PLACEHOLDER)
    block = f"{SUMMARY_BEGIN}\n{body}\n{SUMMARY_END}"
    path = os.path.join(workspace, BRIEF_FILENAME)
    try:
        existing = read_brief(workspace)
        if SUMMARY_BEGIN in existing and SUMMARY_END in existing:
            pre = existing[: existing.index(SUMMARY_BEGIN)]
            post = existing[existing.index(SUMMARY_END) + len(SUMMARY_END):]
            new = pre + block + post
        elif ACTIVITY_HEADER in existing:
            head, _, tail = existing.partition(ACTIVITY_HEADER)
            new = f"{head.rstrip()}\n\n## Summary\n{block}\n\n{ACTIVITY_HEADER}{tail}"
        else:
            new = existing.rstrip() + f"\n\n## Summary\n{block}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as exc:
        logger.warning("[workspace] could not update summary: %s", exc)


def read_summary(workspace: str) -> str:
    """The current auto-maintained summary text (without markers), or '' if unset
    / still the placeholder."""
    existing = read_brief(workspace)
    if SUMMARY_BEGIN not in existing or SUMMARY_END not in existing:
        return ""
    body = existing[existing.index(SUMMARY_BEGIN) + len(SUMMARY_BEGIN): existing.index(SUMMARY_END)].strip()
    return "" if body == _SUMMARY_PLACEHOLDER else body


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


def list_files_meta(workspace: str) -> list[dict]:
    """
    File browser listing (P-0016 b): relative path + size + mtime for each
    workspace file the user authored or the agent generated. Excludes the .git
    internals and the SESSION.md brief (an internal agent artifact, not user
    content — surfaced as History/brief elsewhere, not as a build output).
    """
    out: list[dict] = []
    root = os.path.abspath(workspace)
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel == BRIEF_FILENAME:
                continue
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            out.append({"path": rel, "size": st.st_size, "modified": st.st_mtime})
    return sorted(out, key=lambda e: e["path"])


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
    files = await commit_changed_files(workspace, sha)
    return {
        "commit": sha,
        "short": short.strip(),
        "message": message,
        "diffstat": diffstat,
        "diff": diff,
        "files": files,
    }


async def commit_snapshot(workspace: str, *, message: str) -> Optional[dict]:
    """
    Commit the current workspace state as a version and return the full version
    dict {commit, short, message, diffstat, diff, files} — or None if nothing
    changed. Used by the web-TTY terminal lane (D-0017 thread 2): the human-driven
    CLI edits the workspace with no engine commit boundary, so we snapshot on
    demand (Capture button) or on session stop and surface the artifacts as the
    turn result, exactly like commit_turn does for the chat lane.
    """
    await _git(workspace, "add", "-A")
    code, _ = await _git_out(workspace, "diff", "--cached", "--quiet")
    if code == 0:
        return None
    await _git(workspace, "commit", "-q", "-m", message)
    _, sha = await _git_out(workspace, "rev-parse", "HEAD")
    sha = sha.strip()
    if not sha:
        return None
    _, short = await _git_out(workspace, "rev-parse", "--short", "HEAD")
    return {
        "commit": sha,
        "short": short.strip(),
        "message": message,
        "diffstat": await _commit_diffstat(workspace, sha),
        "diff": await _commit_diff(workspace, sha),
        "files": await commit_changed_files(workspace, sha),
    }


async def commit_paths(workspace: str, *, message: str) -> Optional[str]:
    """
    Stage the whole workspace and commit, returning the new commit sha (or None if
    nothing changed). Used for out-of-turn changes like asset uploads (M1.5) — the
    engine owns the commit boundary (D-0008 C), so an upload becomes a version that
    shows up in Undo/History exactly like a turn's edits.
    """
    await _git(workspace, "add", "-A")
    code, _ = await _git_out(workspace, "diff", "--cached", "--quiet")
    if code == 0:
        return None
    await _git(workspace, "commit", "-q", "-m", message)
    _, sha = await _git_out(workspace, "rev-parse", "HEAD")
    return sha.strip() or None


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


# Map git's status letter to the user-facing word (D-0017 thread 2). "git" is
# never named to the user; these become "added / changed / removed" in the UI.
_STATUS_WORD = {"A": "added", "M": "changed", "D": "removed"}


async def commit_changed_files(workspace: str, sha: str) -> list[dict]:
    """
    Per-file artifact list for a version (D-0017 thread 2): the files a turn
    actually produced, with line counts. This is the *result* of a turn — the
    workspace artifacts — surfaced instead of (or above) scraped agent text.

    Each entry: {path, status, additions, deletions}. status ∈ added/changed/
    removed. Binary files report additions/deletions = None. The SESSION.md brief
    is excluded — it's an internal ledger that churns every turn, not a build
    output (mirrors list_files_meta). Rename detection is off, so a rename shows
    as a remove + add (simpler and honest about what's on disk).
    """
    # name-status gives the change type; numstat gives the line counts. Merge by
    # path so we get both without rename-path parsing ambiguity.
    _, name_out = await _git_out(
        workspace, "show", "--no-color", "--format=", "--name-status", sha
    )
    status_by_path: dict[str, str] = {}
    for line in name_out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            status_by_path[parts[-1]] = _STATUS_WORD.get(parts[0][0], "changed")

    _, num_out = await _git_out(
        workspace, "show", "--no-color", "--format=", "--numstat", sha
    )
    counts_by_path: dict[str, tuple[Optional[int], Optional[int]]] = {}
    order: list[str] = []
    for line in num_out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        adds_s, dels_s, path = parts
        # numstat uses "-" for binary files.
        adds = None if adds_s == "-" else int(adds_s)
        dels = None if dels_s == "-" else int(dels_s)
        counts_by_path[path] = (adds, dels)
        order.append(path)

    files: list[dict] = []
    for path in order:
        if path == BRIEF_FILENAME:
            continue
        adds, dels = counts_by_path[path]
        files.append({
            "path": path,
            "status": status_by_path.get(path, "changed"),
            "additions": adds,
            "deletions": dels,
        })
    return files


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


async def head_commit(workspace: str) -> Optional[str]:
    """The current HEAD commit sha, or None if the workspace has no commits."""
    code, out = await _git_out(workspace, "rev-parse", "HEAD")
    sha = out.strip()
    return sha if code == 0 and sha else None


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
        "files": await commit_changed_files(workspace, commit),
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
