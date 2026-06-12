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

from app.sessions import workspace as ws

# Files served when a directory (or the root) is requested, in order.
_INDEX_FILES = ("index.html", "index.htm")

# Build-output directories: when one of these holds an index.html at the workspace
# root, the preview serves from it instead of the root. A bundled project's root
# index.html is the *source* template (e.g. Vite's, pointing at /src/main.tsx,
# which a browser can't run) — the built site lives in dist/ (or build/, etc.).
# "public" is deliberately absent: it's a source-asset dir, not build output.
_BUILD_DIRS = ("dist", "build", "out", "_site")

# Extensions whose true MIME type the *preview* must keep: browsers refuse to
# apply stylesheets and (especially module) scripts served as text/plain, which
# silently strips all styling/behaviour from a previewed site.
_PREVIEW_MEDIA = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

# Extensions mimetypes either guesses wrong (or as a download-y type) but that we
# want the browser/preview pane to treat as readable UTF-8 text (D-0028): code and
# config files, plus markdown (mimetypes → text/markdown, which some browsers
# offer to download rather than render). Served as text/plain; charset=utf-8.
_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".py", ".js", ".jsx", ".ts", ".tsx", ".css",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".env",
    ".sql", ".rb", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".xml", ".csv",
    ".gitignore",
}


def guess_media_type(path: str) -> str:
    """
    MIME type for a workspace file, tuned for in-browser preview (D-0028):
      - known text/code/markdown extensions → text/plain; charset=utf-8 (renders
        inline instead of downloading; lets the preview pane fetch it as text);
      - everything else → mimetypes' guess (images get image/*), falling back to
        application/octet-stream so an unknown binary is offered as a download.
    """
    ext = os.path.splitext(path)[1].lower()
    # `.gitignore` has no extension via splitext; match on basename too.
    if ext in _TEXT_EXTENSIONS or os.path.basename(path).lower() in _TEXT_EXTENSIONS:
        return "text/plain; charset=utf-8"
    media, _ = mimetypes.guess_type(path)
    return media or "application/octet-stream"


class PreviewError(Exception):
    """Raised with an HTTP-ish status for the route to translate."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def guess_preview_media_type(path: str) -> str:
    """
    MIME type for the *rendered* preview: stylesheets/scripts/JSON keep their real
    type (text/plain CSS/JS is rejected by browsers — see _PREVIEW_MEDIA); the
    rest follows guess_media_type.
    """
    ext = os.path.splitext(path)[1].lower()
    return _PREVIEW_MEDIA.get(ext) or guess_media_type(path)


def _preview_root(workspace: str) -> str:
    """The directory the preview serves from: a build-output dir if one exists."""
    root = os.path.abspath(workspace)
    for name in _BUILD_DIRS:
        if os.path.isfile(os.path.join(root, name, "index.html")):
            return os.path.join(root, name)
    return root


def resolve_preview_file(workspace: str, relpath: str) -> tuple[str, str]:
    """
    Resolve a preview request to (absolute_file_path, media_type).

    Paths resolve under the build-output dir when one exists (so a bundled
    project previews its built site, not its source template), falling back to
    the workspace root for anything not present there (explicit paths like
    `dist/index.html`, root-level images, …).

    Raises PreviewError(404) for escapes / missing files / empty directories.
    """
    relpath = (relpath or "").lstrip("/")
    root = _preview_root(workspace)
    try:
        target = ws.safe_join(root, relpath) if relpath else root
        if relpath and not os.path.exists(target) and root != os.path.abspath(workspace):
            target = ws.safe_join(workspace, relpath)
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

    return target, guess_preview_media_type(target)


# src/href attributes with a root-absolute URL ("/assets/…") — but not
# protocol-relative ("//cdn…") — in served preview HTML.
_ROOT_URL_ATTR = re.compile(r"""(\s(?:src|href)=["'])/(?!/)""")


def rewrite_html_root_paths(html: str, base: str) -> str:
    """
    Prefix root-absolute src/href URLs in preview HTML with the preview base.

    Bundlers default to absolute asset URLs (`<script src="/assets/index-x.js">`),
    which escape the token-carrying preview base and 404 — the page renders blank.
    Relative URLs already resolve under the base and are left alone.
    """
    return _ROOT_URL_ATTR.sub(lambda m: m.group(1) + base.rstrip("/") + "/", html)


def check_token(expected: str | None, provided: str | None) -> None:
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
    return target, guess_media_type(target)


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
