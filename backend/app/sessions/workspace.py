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
import contextlib
import json
import logging
import os

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()


@contextlib.contextmanager
def group_writable():
    """Temporarily apply a group-write umask (002) for files/dirs created in a
    shared session workspace, so they stay co-writable by both `batond` (the
    backend: git/commit/restore, uploads, imports) and the `sandbox`-user agent
    (file edits) — P-0022/D-0020. The session tree is setgid `agents`, so new
    entries inherit the group; the umask preserves the group-write bit the
    backend's default (~022) would otherwise drop. No effect where the group is
    absent (local/dev). umask is process-global, so callers must not `await`
    inside the block — wrap only synchronous makedirs/open/write regions.
    """
    prev = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(prev)

BRIEF_FILENAME = "SESSION.md"

# Markers for the auto-maintained summary block (D-0017 thread 1). Everything
# between them is owned by the summarizer (deterministic placeholder until an LLM
# summary runs); content outside is left untouched.
SUMMARY_BEGIN = "<!-- BATONKEEP:SUMMARY -->"
SUMMARY_END = "<!-- /BATONKEEP:SUMMARY -->"
ACTIVITY_HEADER = "## Activity"
# The brief (SESSION.md) is read in full into every turn prompt and each CLI launch
# file, so the per-turn Activity log can't grow without bound. Keep only the recent
# tail; older turns live in git history and roll up into the `## Summary` section.
ACTIVITY_MAX_ENTRIES = 30
ACTIVITY_TRIM_MARKER = "_(… earlier turns trimmed — see History / the Summary above)_"
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


class WorkspaceRepoError(RuntimeError):
    """The workspace repository is not one the control plane can act on.

    Distinct from "this workspace has no versions yet": the repo is missing,
    unreadable, or *not ours* — an agent replaced it (P-0079). Versioning is
    best-effort for transient git hiccups, but a structural fault must not read
    out as an empty history, because that is indistinguishable from a session
    that did no work.
    """


# Git's dubious-ownership refusal. The control plane owns the workspace repo, so
# seeing this means the `.git` we are reading is not the one we created — the
# observed shape is an agent renaming ours aside and running its own `git init`
# (R3-D2: two sessions, one with `.git_old` left behind, one without).
_DUBIOUS = "detected dubious ownership"
_NOT_A_REPO = "not a git repository"


async def repo_status(workspace: str) -> str:
    """`ok` · `missing` · `foreign` · `broken` — is this repo one we can act on?

    Cheap (`git rev-parse --git-dir`) and safe to call before any read that would
    otherwise report an empty history.
    """
    if not workspace or not os.path.isdir(workspace):
        return "missing"
    if not os.path.isdir(os.path.join(workspace, ".git")):
        return "missing"
    code, out, err = await _git_raw(workspace, "rev-parse", "--git-dir")
    if code == 0:
        return "ok"
    lowered = err.lower()
    if _DUBIOUS in lowered:
        return "foreign"
    if _NOT_A_REPO in lowered:
        return "broken"
    return "broken"


async def require_ours(workspace: str) -> None:
    """Raise WorkspaceRepoError unless the workspace repo is ours to read."""
    status = await repo_status(workspace)
    if status == "ok":
        return
    if status == "foreign":
        raise WorkspaceRepoError(
            "workspace repository was replaced by the agent and is not readable by "
            "Batonkeep — its history is not part of this session's versions"
        )
    if status == "broken":
        raise WorkspaceRepoError("workspace repository is present but unreadable")
    raise WorkspaceRepoError("workspace has no repository")


def flagged_repos() -> list[dict]:
    """Workspaces the boot provenance gate flagged this start (P-0079 item 4).

    Written by `scripts/repair-workspace-repo.py --boot-scan` from `entrypoint.sh`
    *before* the tree is re-owned, because the re-own is what erases the evidence.
    Empty when the file is absent, which is the normal case.
    """
    path = os.environ.get("REPO_PROVENANCE_REPORT", "/data/repo-provenance.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("flagged", [])
    except (OSError, ValueError):
        return []


async def _git(workspace: str, *args: str) -> None:
    """Run a git command in the workspace; log (don't raise) on failure."""
    await _git_out(workspace, *args)


async def _git_raw(workspace: str, *args: str) -> tuple[int, str, str]:
    """Run a git command, returning (returncode, stdout, stderr) without logging."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", workspace, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _git_out(workspace: str, *args: str) -> tuple[int, str]:
    """
    Run a git command and capture stdout. Returns (returncode, stdout). Logs (does
    not raise) on non-zero exit so the session loop is never broken by a git
    hiccup — versioning is best-effort over the turn lifecycle.

    Best-effort applies to *transient* failures. A structural one — the repo is
    not ours — logs at ERROR, because callers that fall back to an empty result
    would otherwise present "no versions" for a workspace full of committed work.
    """
    code, out, err = await _git_raw(workspace, *args)
    if code != 0:
        stderr = err.strip()
        if _DUBIOUS in stderr.lower():
            logger.error(
                "[workspace] git %s refused %s: the repository is not ours — an agent "
                "replaced it. Its commits are NOT this session's versions.", args, workspace
            )
        else:
            logger.warning("[workspace] git %s failed: %s", args, stderr)
    return code, out


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
    # The parent /data/sessions is setgid `agents`, so new subdirs inherit the
    # group; the group-write umask + setgid on the root keep the tree co-writable.
    with group_writable():
        os.makedirs(root, exist_ok=True)
        try:
            os.chmod(root, 0o2770)  # setgid + group rwx
        except OSError as exc:  # best-effort: local/dev may not have the group
            logger.debug("[workspace] chmod 2770 %s skipped: %s", root, exc)

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
        # `--shared=group` is load-bearing, not hygiene (P-0079). Everything else in
        # the workspace is co-writable by `batond` and the `sandbox` agent, but git
        # creates `.git` 0755 owner-only regardless of umask — so an agent that runs
        # `git commit` itself is denied inside the one directory that matters. The
        # observed response was to rename our repo aside and `git init` its own,
        # after which the control plane could read neither its commits nor ours.
        # Share the repo with the `agents` group and the agent has no reason to.
        await _git(root, "init", "-q", "--shared=group")
        # Local identity so commits work without global git config.
        await _git(root, "config", "user.email", "agent@batonkeep.local")
        await _git(root, "config", "user.name", "batonkeep")
        _seed_git_exclude(root)  # keep toolchain trees out of every commit (P-0081)
        await _git(root, "add", "-A")
        await _git(root, "commit", "-q", "-m", "session: initialise workspace")
    else:
        _seed_git_exclude(root)
        await ensure_repo_shared(root)
    return root


def _seed_git_exclude(workspace: str) -> None:
    """Write `_PRUNE_DIRS` into `.git/info/exclude` so `git add -A` never stages a
    dependency install or tool cache (P-0081, R3-D4).

    A cold-continuity turn produced 867 changed-file rows for three real outputs —
    a `.venv` the agent built, staged whole because nothing excluded it. This is
    the repo-local, agent-invisible exclusion: unlike a workspace `.gitignore` it is
    not a file the agent can clobber or that pollutes the deliverable, and unlike
    the listing's in-memory prune (`_PRUNE_DIRS` at read time) it stops the rows at
    the commit boundary, where the diff/version surface is actually built. The fact
    that an environment was created is not lost — `present_toolchain_dirs()` records
    it as one line per tree instead of one row per file. Best-effort: a repo whose
    `.git` we cannot write is a P-0079 provenance problem surfaced elsewhere.
    """
    info_dir = os.path.join(workspace, ".git", "info")
    patterns = sorted(d for d in _PRUNE_DIRS if d != ".git")
    body = (
        "# Seeded by Batonkeep (P-0081): dependency installs and tool caches are\n"
        "# kept out of turn diffs. Their creation is recorded separately, per-tree.\n"
        + "".join(f"{name}/\n" for name in patterns)
    )
    try:
        with group_writable():
            os.makedirs(info_dir, exist_ok=True)
            with open(os.path.join(info_dir, "exclude"), "w", encoding="utf-8") as f:
                f.write(body)
    except OSError as exc:  # dev boxes, foreign .git, odd mounts — non-fatal
        logger.debug("[workspace] seed .git/info/exclude %s skipped: %s", workspace, exc)


def present_toolchain_dirs(workspace: str) -> list[str]:
    """Names from `_PRUNE_DIRS` (excluding `.git`) that exist in the workspace — the
    signal `_seed_git_exclude` deliberately drops from the diff (P-0081, R3-D4).

    Recorded on a version so "this turn also built a `.venv`" survives even though
    its 800 files do not appear as changes. Cheap: prunes into found trees, so a
    populated `node_modules` is one stat, not a walk of its contents.
    """
    root = os.path.abspath(workspace)
    found: set[str] = set()
    excludable = _PRUNE_DIRS - {".git"}
    for dirpath, dirnames, _ in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        hit = [d for d in dirnames if d in excludable]
        found.update(hit)
        # don't descend into a matched tree — its name is already recorded
        dirnames[:] = [d for d in dirnames if d not in excludable]
    return sorted(found)


async def ensure_repo_shared(workspace: str) -> bool:
    """Make an existing workspace repo group-writable. Idempotent; returns True if changed.

    Workspaces created before P-0079 have a `.git` the agent cannot write, which is
    what provoked the displacement. Repairing them on access closes the window for
    sessions that already exist. Only touches a repo that is still **ours** — a
    foreign one is a reporting problem, not something to silently adopt.
    """
    if await repo_status(workspace) != "ok":
        return False
    _, current = await _git_out(workspace, "config", "--get", "core.sharedRepository")
    if current.strip() in ("group", "1", "true"):
        return False
    await _git(workspace, "config", "core.sharedRepository", "group")
    git_dir = os.path.join(workspace, ".git")
    changed = False
    for dirpath, dirnames, filenames in os.walk(git_dir):
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            try:
                mode = os.stat(path).st_mode & 0o7777
                # Mirror the owner's read/write bits to the group; setgid on dirs so
                # new objects keep the shared group.
                want = mode | ((mode & 0o600) >> 3)
                if os.path.isdir(path):
                    want |= 0o2010 if mode & 0o100 else 0o2000
                if want != mode:
                    os.chmod(path, want)
                    changed = True
            except OSError as exc:  # best-effort: dev boxes, races, odd mounts
                logger.debug("[workspace] chmod %s skipped: %s", path, exc)
    try:
        mode = os.stat(git_dir).st_mode & 0o7777
        os.chmod(git_dir, mode | 0o2070)
        changed = True
    except OSError as exc:
        logger.debug("[workspace] chmod %s skipped: %s", git_dir, exc)
    if changed:
        logger.info("[workspace] repo %s made group-writable (P-0079)", workspace)
    return changed


def read_brief(workspace: str) -> str:
    """Return the SESSION.md brief text, or '' if absent."""
    path = os.path.join(workspace, BRIEF_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _format_files(files: list[dict] | None) -> str:
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
    files: list[dict] | None = None,
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
    entry = (
        f"- **turn {seq}** · {provider} · {lane} — {headline} — changed: {_format_files(files)}"
    )

    path = os.path.join(workspace, BRIEF_FILENAME)
    try:
        existing = read_brief(workspace)
        if ACTIVITY_HEADER in existing:
            head, _, tail = existing.partition(ACTIVITY_HEADER)
            # Existing entries are the bullet lines; this also drops the "(none yet)"
            # placeholder and any prior trim marker. The brief feeds every turn's
            # prompt + each CLI launch file (read_brief), so the per-turn log must be
            # bounded — keep the recent tail; the rolling `## Summary` carries the
            # durable rollup and git history has the full record.
            entries = [ln for ln in tail.splitlines() if ln.lstrip().startswith("- ")]
            entries.append(entry)
            trimmed = len(entries) > ACTIVITY_MAX_ENTRIES
            kept = entries[-ACTIVITY_MAX_ENTRIES:]
            body = "\n".join(kept)
            marker = f"{ACTIVITY_TRIM_MARKER}\n" if trimmed else ""
            new = f"{head}{ACTIVITY_HEADER}\n{marker}{body}\n"
        else:
            # Older ledger without the section (or a hand-edited brief): append one.
            new = existing.rstrip() + f"\n\n{ACTIVITY_HEADER}\n{entry}\n"
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
    body = existing[
        existing.index(SUMMARY_BEGIN) + len(SUMMARY_BEGIN): existing.index(SUMMARY_END)
    ].strip()
    return "" if body == _SUMMARY_PLACEHOLDER else body


# Directory names pruned from the workspace listing: dependency installs and
# tool caches. Listing these bloats the session context — a `node_modules` after
# `npm install` is ~13k files, enough to blow the agent launch past ARG_MAX
# ("[Errno 7] Argument list too long") — and they are noise the user never wants
# to browse. We prune by directory NAME rather than honouring `.gitignore`,
# because `.gitignore` also lists build OUTPUT (`dist/`, `build/`) — and that
# output is the deliverable: it's what the file browser shows, what preview
# renders, and what publish serves, so it must stay visible. Deps out, build in.
_PRUNE_DIRS = frozenset({
    ".git", "node_modules", ".pnpm-store", "bower_components",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".cache", ".parcel-cache", ".gradle", "vendor",
})


def _listed_paths(workspace: str) -> list[str]:
    """
    Relative paths of the workspace's source/content/build files — everything the
    user authored, the agent generated, or the build emitted, EXCLUDING dependency
    installs and tool caches (`_PRUNE_DIRS`). Build output (`dist/`, `build/`) is
    deliberately kept: it's the deliverable the browser/preview/publish surface.
    """
    root = os.path.abspath(workspace)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for name in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return out


# Pointer (not a paste) to how the agent should learn the workspace contents.
# Both lanes (chat/API and terminal/CLI) have shell/file tools and the workspace
# is a git repo, so the agent discovers files on demand — keeping the prompt small
# and never stale. This replaces the old enumerated `## Workspace files` block,
# whose post-`npm install` size blew the CLI launch past ARG_MAX.
WORKSPACE_DISCOVERY = (
    "## Workspace files\n"
    "This workspace is a git repository and the source of truth. Discover its "
    "current contents with your own tools — `git ls-files` for tracked source, "
    "`git status`/`ls`/`find` for untracked or build output — rather than relying "
    f"on a static list. `{BRIEF_FILENAME}` holds the brief + running state; read "
    "it and the relevant files, then continue from the current state."
)


def list_files_meta(workspace: str) -> list[dict]:
    """
    File browser listing (P-0016 b): relative path + size + mtime for each
    workspace file the user authored, the agent generated, or the build emitted.
    Excludes dependency installs / tool caches (`_PRUNE_DIRS`) and the SESSION.md
    brief (an internal agent artifact, not user content — surfaced as
    History/brief elsewhere, not as a build output).
    """
    root = os.path.abspath(workspace)
    out: list[dict] = []
    for rel in _listed_paths(workspace):
        if rel == BRIEF_FILENAME:
            continue
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            continue
        out.append({"path": rel, "size": st.st_size, "modified": st.st_mtime})
    return sorted(out, key=lambda e: e["path"])


# How many prior turns to replay for conversational continuity (D-0008 keeps the
# workspace as the source of truth; this just preserves the immediate dialogue so
# short follow-ups like "yes, do that" resolve their referent instead of being
# answered cold). Kept small so the workspace, not the transcript, stays primary.
RECENT_TURNS = 4
# Cap each replayed message so a huge prior turn can't dominate the prompt.
_RECENT_MSG_CHARS = 1500


def _clip(text: str, limit: int = _RECENT_MSG_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " …"


def build_turn_context(
    workspace: str, user_message: str, recent_turns: list[tuple[str, str]] | None = None
) -> str:
    """
    Assemble the prompt for a turn from the *workspace*, not a replayed transcript
    (D-0008). This is what lets a switched-in agent continue seamlessly: it sees
    the SESSION.md brief + the current file list + the new user message.

    `recent_turns` is an ordered list of `(user_message, agent_response)` for the
    last few exchanges, included so conversational follow-ups ("yes, do that")
    keep their referent. The workspace remains the source of truth; this is a
    bounded dialogue tail, not the full transcript.

    We do NOT enumerate the workspace files here. The agent has exec/file tools and
    the workspace is a git repo, so it discovers files itself (`git ls-files`, etc.)
    — a static listing both bloats the prompt (a post-`npm install` tree is ~13k
    paths) and goes stale the moment the agent writes a file. We point, not paste.
    """
    brief = read_brief(workspace)
    convo = ""
    if recent_turns:
        lines = []
        for u, a in recent_turns[-RECENT_TURNS:]:
            lines.append(f"User: {_clip(u)}")
            if a:
                lines.append(f"Assistant: {_clip(a)}")
        convo = "## Recent conversation\n" + "\n".join(lines) + "\n\n"
    return (
        f"{brief}\n\n"
        f"{WORKSPACE_DISCOVERY}\n\n"
        f"{convo}"
        f"{GITIGNORE_GUIDANCE}\n\n"
        f"{PYTHON_DEPS_GUIDANCE}\n\n"
        f"{PACKAGING_CADENCE_GUIDANCE}\n\n"
        f"## User message\n{user_message}\n"
    )


# Agent instruction to keep the workspace clean (D-0029 part 1): whenever the agent
# creates package/build directories (installs deps, compiles), it should add them to
# .gitignore so they don't get committed per-turn, clutter the file listing, or ride
# along in the download/share bundle. The agent owns this file; we never seed a fixed
# baseline (it adapts to non-standard install paths — D-0029).
GITIGNORE_GUIDANCE = (
    "## Keeping the workspace clean\n"
    "Maintain a `.gitignore` in the workspace root. If you install packages or "
    "generate build artifacts (e.g. `node_modules/`, `.venv/`, `__pycache__/`, "
    "`dist/`, `build/`, or any package/dependency directory), add those paths to "
    "`.gitignore` so they are not tracked. Keep only source and content files "
    "tracked — the user's downloads and shared site should not contain dependencies."
)


# Standardised Python-dependency workflow (D-0029 follow-up): without an explicit
# instruction, some agents (observed: agy) install packages into an ad-hoc directory
# (e.g. `packages/`) that the gitignore/publish-exclusion lists don't recognise, so it
# leaks into the file listing and the share/download bundle. Pin everyone to a `.venv`
# (already on the exclusion lists) installed with uv, recorded in requirements.txt so
# scheduled re-runs / fresh environments are reproducible.
PYTHON_DEPS_GUIDANCE = (
    "## Installing Python packages\n"
    "If you need Python packages, use a **virtualenv at `.venv/` in the workspace** "
    "— do not install into a custom directory (e.g. `packages/`) or system-wide:\n"
    "```\n"
    "uv venv .venv            # or: python -m venv .venv\n"
    "source .venv/bin/activate\n"
    "uv pip install <pkgs>    # or: pip install <pkgs>\n"
    "```\n"
    "Record dependencies in `requirements.txt` so the work stays reproducible. The "
    "`.venv/` is ephemeral and gitignored — if a missing-module error shows the "
    "environment was reset, recreate it and reinstall from `requirements.txt`."
)


# Checkpoint cadence (P-0069 item 4c). From the P49 chaos-drill: one explicit
# checkpoint at a phase boundary raised a cold auditor's recovery grade D→B and
# turned an interrupt from full-redo into salvage-and-continue. The engine
# auto-commits every turn, so the actionable practice is to work in phase-sized
# turns — don't defer every write to the end of one long turn, where an interrupt
# or the turn timeout loses all uncheckpointed progress (slice A's cancel-snapshot
# now captures what landed, but only committed phases are clean recovery points).
PACKAGING_CADENCE_GUIDANCE = (
    "## Checkpoint at phase boundaries\n"
    "Work in phase-sized steps. Each turn's file changes are committed as a durable "
    "checkpoint automatically, so when the work has several deliverables, finish and "
    "write out each coherent phase — then state what is done and what is next — rather "
    "than deferring all output to the very end. Incremental checkpoints make an "
    "interruption salvage-and-continue instead of a full redo."
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
) -> dict | None:
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
        "environments": present_toolchain_dirs(workspace),
    }


async def commit_snapshot(workspace: str, *, message: str) -> dict | None:
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
        "environments": present_toolchain_dirs(workspace),
    }


async def commit_paths(workspace: str, *, message: str) -> str | None:
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
    counts_by_path: dict[str, tuple[int | None, int | None]] = {}
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


async def tracked_files(workspace: str) -> set[str]:
    """Set of file paths tracked at HEAD (`git ls-files`) — this session's committed
    tree. Used by the free default output check (P-0069 item 6) to verify an agent's
    claimed artifacts actually landed here. Best-effort: an empty set on git error
    (the check then simply finds nothing, never a false positive)."""
    code, out = await _git_out(workspace, "ls-files")
    if code != 0:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


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


async def head_commit(workspace: str) -> str | None:
    """The current HEAD commit sha, or None if the workspace has no commits."""
    code, out = await _git_out(workspace, "rev-parse", "HEAD")
    sha = out.strip()
    return sha if code == 0 and sha else None


async def version_diff(workspace: str, commit: str) -> dict | None:
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


async def restore_version(workspace: str, commit: str) -> dict | None:
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
    return (
        bool(commit) and len(commit) <= 40
        and all(c in "0123456789abcdef" for c in commit.lower())
    )
