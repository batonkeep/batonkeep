"""
session_context.py — Filesystem-as-context seeding for the web-TTY terminal lane
(D-0017 thread 1, terminal slice).

A CLI launched in terminal mode starts *cold*: unlike chat turns (which get
build_turn_context()), the web-TTY spawn just drops the binary into the workspace.
To prime it without any prompt injection — keeping the lane a pure terminal
multiplexer (D-0016 ToS posture) — we write the session ledger into the file the
CLI conventionally auto-reads from its working directory (CLAUDE.md / AGENTS.md /
GEMINI.md). The CLI loads it on its own; we never type anything.

The content lives inside a delimited managed block, so a user-authored convention
file (e.g. from an imported repo) is preserved — we only own our block. Switching
providers re-seeds the new CLI's convention file, which is how context transfers
across a provider switch in the terminal lane.

This is the terminal-lane counterpart of build_turn_context() (the chat lane). A
richer auto-summarized ledger is D-0017 thread 1 proper; this v0 reuses the brief
(SESSION.md) + the workspace file list.
"""
from __future__ import annotations

import logging
import os

from app.sessions.workspace import (
    BRIEF_FILENAME,
    GITIGNORE_GUIDANCE,
    list_files,
    read_brief,
)

logger = logging.getLogger(__name__)

# Which file each CLI auto-reads from its working directory. Default to AGENTS.md
# (the cross-tool convention) for providers we haven't mapped explicitly.
#
# agy (Antigravity) auto-loads BOTH AGENTS.md and GEMINI.md, but **AGENTS.md takes
# precedence when both exist** (and Google's migration makes AGENTS.md the repo-root
# standard). So agy maps to AGENTS.md, not GEMINI.md: in a mixed-provider session a
# prior codex/grok turn (or an imported repo) leaves an AGENTS.md, and agy would
# read that stale file over a freshly-seeded GEMINI.md — silently breaking the
# context-transfers-on-switch guarantee. Unifying on AGENTS.md (only claude differs)
# keeps the single seeded block authoritative across every CLI switch.
_CONTEXT_FILES: dict[str, str] = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "agy": "AGENTS.md",
    "grok": "AGENTS.md",
}
_DEFAULT_CONTEXT_FILE = "AGENTS.md"

# Managed-block markers. Everything between them is ours and is overwritten on each
# terminal launch; anything outside is user/agent content and is left untouched.
_BEGIN = "<!-- BATONKEEP:SESSION-CONTEXT (auto-generated — overwritten on each terminal launch; edit elsewhere) -->"  # noqa: E501
_END = "<!-- /BATONKEEP:SESSION-CONTEXT -->"


def context_filename(provider_name: str) -> str:
    """The convention file a given provider CLI auto-reads from the workspace."""
    return _CONTEXT_FILES.get(provider_name, _DEFAULT_CONTEXT_FILE)


def context_filenames() -> set[str]:
    """All CLI convention filenames — for publish/asset exclusion. Includes
    GEMINI.md: we no longer seed it (agy unified onto AGENTS.md), but agy still
    auto-reads a GEMINI.md if one is present, so it stays a known convention file
    we keep out of published bundles."""
    return set(_CONTEXT_FILES.values()) | {_DEFAULT_CONTEXT_FILE, "GEMINI.md"}


def render_session_context(workspace: str) -> str:
    """Render the priming block: brief (SESSION.md) + current workspace files.

    Reused across providers so a switch transfers the same context. Excludes the
    brief file itself and our convention files from the listing (they're noise).
    """
    brief = read_brief(workspace).strip()
    skip = context_filenames() | {BRIEF_FILENAME}
    files = [f for f in list_files(workspace) if f not in skip]
    file_list = "\n".join(f"- {f}" for f in files) if files else "- (none yet)"
    return (
        "# Batonkeep session context\n\n"
        "You are continuing an existing Batonkeep build session in **this** "
        "workspace — not starting fresh. The workspace files are the source of "
        f"truth and `{BRIEF_FILENAME}` holds the brief + running state. Read them "
        "and pick up from the current state.\n\n"
        "## Brief\n"
        f"{brief or '_(no brief recorded yet)_'}\n\n"
        "## Workspace files\n"
        f"{file_list}\n\n"
        f"{GITIGNORE_GUIDANCE}\n"
    )


def _managed_block(workspace: str) -> str:
    return f"{_BEGIN}\n{render_session_context(workspace)}{_END}\n"


def seed_provider_context(workspace: str, provider_name: str) -> str:
    """Write/refresh the session-context managed block into the provider's
    convention file. Non-destructive: preserves any content outside our markers.

    Returns the filename written (relative to the workspace). Best-effort — callers
    should not let a failure here block launching the terminal.
    """
    filename = context_filename(provider_name)
    path = os.path.join(workspace, filename)
    block = _managed_block(workspace)

    existing = ""
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""

    if _BEGIN in existing and _END in existing:
        pre = existing[: existing.index(_BEGIN)]
        post = existing[existing.index(_END) + len(_END):]
        # Replace just our block; keep user content before/after it.
        if post.strip():
            new = pre + block + post.lstrip("\n")
        else:
            new = pre.rstrip("\n") + ("\n\n" if pre.strip() else "") + block
    elif existing.strip():
        # Prepend our block so the CLI sees session context first, keep user content.
        new = block + "\n" + existing
    else:
        new = block

    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    logger.info("seeded terminal context for %s → %s", provider_name, filename)
    return filename
