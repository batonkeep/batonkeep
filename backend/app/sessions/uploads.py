"""
sessions/uploads.py — asset upload-in (M1.5, D-0010).

Files dropped into the chat composer land here as real workspace files, then the
agent references them by name in the conversation (filesystem-as-context, D-0008 A).
Images go to `assets/`, data files (csv/pdf/txt/md) to `data/`. Limits (max size +
extension allowlist) are env-configurable (D-0008 B). Parse depth is **raw
availability** — we just place the bytes; the agent reads images verbatim and
csv/pdf as extracted text. Nothing leaves the backend (sovereignty).

The route commits the workspace after a successful upload, so an upload becomes a
version in Undo/History like any other workspace change.
"""
from __future__ import annotations

import os
import re
from typing import BinaryIO

from app.config import get_settings
from app.sessions.workspace import group_writable, safe_join

_settings = get_settings()

# Extension → destination subdir. Images are referenced verbatim by the agent;
# data files are read/parsed as text.
_IMAGE_EXT = {"png", "jpg", "jpeg", "svg", "webp"}

# Strip anything but a safe filename: drop directory components and collapse the
# rest to a conservative charset so an upload can never traverse or shell-trick.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class UploadError(Exception):
    """Raised on a rejected upload (bad name, disallowed type, too large)."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _sanitize_name(filename: str) -> str:
    base = os.path.basename(filename or "").strip()
    base = base.replace("\x00", "")
    # Collapse to safe chars; guard against names that become empty or dotfiles.
    cleaned = _SAFE_NAME.sub("_", base).strip("._")
    if not cleaned:
        raise UploadError(400, "invalid filename")
    return cleaned[:128]


def _ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def dest_relpath(filename: str) -> str:
    """
    Validate the filename's extension against the allowlist and return the
    workspace-relative destination path (assets/<name> or data/<name>).
    Raises UploadError on a disallowed type.
    """
    name = _sanitize_name(filename)
    ext = _ext_of(name)
    allowed = _settings.upload_allowed_ext_set
    if ext not in allowed:
        raise UploadError(
            415,
            f"file type '.{ext or '?'}' not allowed; permitted: "
            + ", ".join(sorted(allowed)),
        )
    subdir = "assets" if ext in _IMAGE_EXT else "data"
    return f"{subdir}/{name}"


def save_upload(workspace: str, filename: str, src: BinaryIO) -> str:
    """
    Stream an uploaded file into the session workspace, enforcing the size cap.
    Returns the workspace-relative path the agent can reference. Raises UploadError
    on validation failure (and removes any partial file).
    """
    relpath = dest_relpath(filename)
    abs_path = safe_join(workspace, relpath)  # defence-in-depth vs traversal

    max_bytes = _settings.upload_max_bytes
    written = 0
    try:
        # Group-write umask so the asset lands co-writable by the sandbox-user
        # agent (P-0022/D-0020), not just readable — assets/ and data/ inherit the
        # setgid `agents` group from the workspace root. No await inside the block.
        with group_writable():
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as out:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise UploadError(
                            413, f"file exceeds max upload size ({max_bytes} bytes)"
                        )
                    out.write(chunk)
    except UploadError:
        _remove_quiet(abs_path)
        raise
    except OSError as exc:
        _remove_quiet(abs_path)
        raise UploadError(500, f"could not save upload: {exc}") from exc

    if written == 0:
        _remove_quiet(abs_path)
        raise UploadError(400, "empty file")
    return relpath


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
