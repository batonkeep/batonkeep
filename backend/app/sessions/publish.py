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
from typing import Optional

from app.config import get_settings
from app.sessions import workspace as ws

logger = logging.getLogger(__name__)
_settings = get_settings()

# Workspace entries never included in a publish/download (internal, not site assets).
_EXCLUDED_TOP = {".git", ws.BRIEF_FILENAME}


def _publishable_files(workspace: str) -> list[str]:
    """Relative paths of the workspace's static assets (excludes .git, SESSION.md)."""
    out: list[str] = []
    root = os.path.abspath(workspace)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        top = "" if rel_dir == "." else rel_dir.split(os.sep)[0]
        if top in _EXCLUDED_TOP:
            dirnames[:] = []
            continue
        # Prune excluded dirs at the root level before descending.
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_TOP]
        for name in filenames:
            if rel_dir == "." and name in _EXCLUDED_TOP:
                continue
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root))
    return sorted(out)


def publish_token_dir(share_token: str) -> str:
    """Absolute path to a published bundle dir. share_token must be a bare token."""
    if not share_token or "/" in share_token or "\\" in share_token or share_token in (".", ".."):
        raise ValueError(f"unsafe share token: {share_token!r}")
    return os.path.abspath(os.path.join(_settings.publish_dir, share_token))


def build_bundle(workspace: str, share_token: str) -> str:
    """
    Materialize the workspace's static assets into a fresh bundle dir named by the
    share token. Returns the bundle dir path. Replaces any existing bundle.
    """
    dest = publish_token_dir(share_token)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    for rel in _publishable_files(workspace):
        src = ws.safe_join(workspace, rel)
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return dest


def remove_bundle(share_token: Optional[str], path: Optional[str]) -> None:
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
    """Build an in-memory zip of the workspace's static assets (download pack #1)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _publishable_files(workspace):
            zf.write(ws.safe_join(workspace, rel), arcname=rel)
    return buf.getvalue()
