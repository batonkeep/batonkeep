"""
sessions/publish.py — publish + share a session's build (M1.4).

Two delivery mechanisms (D-0009):
  #1 Download pack — zip the workspace's static assets for self-hosting.
  #2 Revocable backend share link — materialize those same assets into a public
     bundle served at /api/share/{token}; revoking removes the bundle (→ 404).

"Static assets" = everything in the workspace EXCEPT git internals (`.git`) and the
internal agent brief (`SESSION.md`), so the published site bundles uploaded images,
css, js, html, etc. while "nothing else in the workspace is exposed" (M1.4 gate).
Publish snapshots the current files into a separate bundle dir, so later turns don't
change the live site until the user re-publishes.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import zipfile

from app.config import get_settings
from app.sessions import workspace as ws
from app.sessions.session_context import context_filenames

logger = logging.getLogger(__name__)
_settings = get_settings()

# Workspace entries never included in a publish/download (internal, not site assets):
# git internals, the agent brief, and the terminal-context convention files
# (CLAUDE.md / AGENTS.md / GEMINI.md) we seed for the web-TTY lane (D-0017).
_EXCLUDED_TOP = {".git", ws.BRIEF_FILENAME} | context_filenames()

# Package-manager / build-artifact directories never worth shipping in a download
# pack or share bundle, pruned at ANY depth (D-0029 part 2). Defence-in-depth: the
# agent is also instructed to .gitignore these (so they never get committed), but
# the bundle stays lean even if a user downloads before the agent has cleaned up,
# or the agent installs to a path it forgot to ignore.
_EXCLUDED_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build",
    ".next", ".nuxt", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".gradle", "target", "vendor", ".tox", "site-packages",
}


def _publishable_files(workspace: str) -> list[str]:
    """Relative paths of the workspace's static assets (excludes .git, SESSION.md,
    and package/build artifact dirs at any depth — D-0029)."""
    out: list[str] = []
    root = os.path.abspath(workspace)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        top = "" if rel_dir == "." else rel_dir.split(os.sep)[0]
        if top in _EXCLUDED_TOP:
            dirnames[:] = []
            continue
        # Prune excluded top-level entries and package/build dirs at every level.
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIRS and not (rel_dir == "." and d in _EXCLUDED_TOP)
        ]
        for name in filenames:
            if rel_dir == "." and name in _EXCLUDED_TOP:
                continue
            full = os.path.join(dirpath, name)
            # Never follow symlinks out of the workspace. build_bundle/zip_workspace
            # run as the backend user (batond), which can read control-plane files
            # the sandbox agent cannot (/data incl. the DB, /app). A symlink planted
            # in the workspace would otherwise be followed and its target copied into
            # a publicly-served share bundle — a sandbox→control-plane leak. The
            # import side skips symlinks too; keep export symmetric. realpath guards
            # against a symlinked parent dir as defence in depth.
            if os.path.islink(full) or not os.path.realpath(full).startswith(root + os.sep):
                continue
            out.append(os.path.relpath(full, root))
    return sorted(out)


def publish_token_dir(share_token: str) -> str:
    """Absolute path to a published bundle dir. share_token must be a bare token."""
    if not share_token or "/" in share_token or "\\" in share_token or share_token in (".", ".."):
        raise ValueError(f"unsafe share token: {share_token!r}")
    return os.path.abspath(os.path.join(_settings.publish_dir, share_token))


def _site_root(workspace: str) -> str:
    """
    The directory to publish: the build-output dir when one exists (mirrors the
    preview's site_root), else the workspace root. A bundled project's site lives
    in dist/ — publishing the workspace root would ship the *source* (and exclude
    dist via _EXCLUDED_DIRS), leaving the shared link blank while the preview works.
    """
    from app.sessions.preview import site_root

    return site_root(workspace)


def build_bundle(workspace: str, share_token: str) -> str:
    """
    Materialize the workspace's static assets into a fresh bundle dir named by the
    share token. Returns the bundle dir path. Replaces any existing bundle.
    Publishes from the build-output dir when one exists (see _site_root).
    """
    dest = publish_token_dir(share_token)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    root = _site_root(workspace)
    for rel in _publishable_files(root):
        src = ws.safe_join(root, rel)
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return dest


def remove_bundle(share_token: str | None, path: str | None) -> None:
    """Remove a published bundle dir (revoke). Best-effort; never raises."""
    target = path
    if not target and share_token:
        try:
            target = publish_token_dir(share_token)
        except ValueError:
            return
    if target and os.path.isdir(target):
        shutil.rmtree(target, ignore_errors=True)


def zip_workspace(workspace: str) -> bytes:
    """Build an in-memory zip of the static assets (download pack #1) — the
    self-hostable site, so the build-output dir when one exists (see _site_root)."""
    buf = io.BytesIO()
    root = _site_root(workspace)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _publishable_files(root):
            zf.write(ws.safe_join(root, rel), arcname=rel)
    return buf.getvalue()
