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
