"""
sessions/preview.py — live preview of a session's workspace (M1.2).

Serves the static files an agent built in the sandboxed workspace (HTML/CSS/JS,
images) so they render in the in-UI preview pane. Every request is gated by the
session's unguessable preview token, and paths are resolved inside the workspace
only (path-traversal safe) — the workspace is never reachable without session
auth, and one session can never serve another's files (sandbox-isolation skill).

M1.2 serves static files directly (the landing-page demo builds static output).
Detecting/launching a long-running workspace dev server and proxying it graduates
later; the route shape stays the same.
"""
from __future__ import annotations

import mimetypes
import os
import re
from typing import Optional

from app.sessions import workspace as ws

# Files served when a directory (or the root) is requested, in order.
_INDEX_FILES = ("index.html", "index.htm")


class PreviewError(Exception):
    """Raised with an HTTP-ish status for the route to translate."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def resolve_preview_file(workspace: str, relpath: str) -> tuple[str, str]:
    """
    Resolve a preview request to (absolute_file_path, media_type).

    Raises PreviewError(404) for escapes / missing files / empty directories.
    """
    relpath = (relpath or "").lstrip("/")
    try:
        target = ws.safe_join(workspace, relpath) if relpath else os.path.abspath(workspace)
    except ValueError:
        # Path traversal attempt — treat as not found (don't confirm the escape).
        raise PreviewError(404, "Not found")

    if os.path.isdir(target):
        for name in _INDEX_FILES:
            candidate = os.path.join(target, name)
            if os.path.isfile(candidate):
                target = candidate
                break
        else:
            raise PreviewError(404, "No index file in this directory")

    if not os.path.isfile(target):
        raise PreviewError(404, "Not found")

    media, _ = mimetypes.guess_type(target)
    return target, media or "application/octet-stream"


def check_token(expected: Optional[str], provided: Optional[str]) -> None:
    """Raise PreviewError(403) unless a non-empty token matches exactly."""
    if not expected or not provided or provided != expected:
        raise PreviewError(403, "Invalid or missing preview token")


def resolve_workspace_file(workspace: str, relpath: str) -> tuple[str, str]:
    """
    Resolve a raw file-browser request to (absolute_file_path, media_type).

    Unlike resolve_preview_file, this serves the *exact* file with NO index.html
    fallback — so a non-web artifact (a .py script, a .csv, a .json) is returned
    verbatim for view/download. Path-traversal safe; raises PreviewError(404) for
    escapes, directories, or missing files.
    """
    relpath = (relpath or "").lstrip("/")
    if not relpath:
        raise PreviewError(404, "Not found")
    try:
        target = ws.safe_join(workspace, relpath)
    except ValueError:
        # Path traversal attempt — treat as not found (don't confirm the escape).
        raise PreviewError(404, "Not found")
    if not os.path.isfile(target):
        raise PreviewError(404, "Not found")
    media, _ = mimetypes.guess_type(target)
    return target, media or "application/octet-stream"


def rewrite_workspace_file_links(text: str, session_id: str, workspace: str) -> str:
    """
    Rewrite an agent's absolute `file://<workspace>/<rel>` links to the session's
    token-free, owner-scoped raw-file route so they resolve in the browser
    (P-0016 b). Agents reference generated artifacts with the workspace's on-disk
    path (e.g. `[download_data.py](file:///data/sessions/<id>/download_data.py)`),
    which dead-ends in a browser; this maps them to `/api/sessions/<id>/files/raw/<rel>`.

    Only this session's own workspace path is rewritten — unrelated `file://`
    links are left untouched. Match stops at whitespace and markdown/HTML
    delimiters so the surrounding `[label](…)` syntax is preserved.
    """
    if not text:
        return text
    root = os.path.abspath(workspace).rstrip("/")
    # file://<root>/<rel> — root starts with "/", so this also covers file:///… .
    pattern = re.compile(r"file://" + re.escape(root) + r"(/[^\s)\]\"'>]*)")
    base = f"/api/sessions/{session_id}/files/raw"
    return pattern.sub(lambda m: base + m.group(1), text)
