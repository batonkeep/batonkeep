"""
sessions/imports.py — import an existing site into a session workspace.

Unlike upload-in (M1.5), which flattens names into assets/ or data/ buckets, this
extracts a **.zip or .tar(.gz/.bz2/.xz)** archive into the workspace **root**,
preserving the directory structure — so an existing multi-file site (with its
folders and relative links intact) becomes the session's starting point. The agent
then continues from it via filesystem-as-context (D-0008); the route commits the
import as one version (Undo/History).

The session has its own engine-owned git history, so a `.git/` directory in the
archive is dropped (and SESSION.md is protected) — importing files, not history.

Safety: every entry is path-traversal checked (zip-slip / tar-slip), non-regular
entries (symlinks, hardlinks, devices) are skipped, and total size + file count are
capped to defuse archive bombs.
"""
from __future__ import annotations

import os
import tarfile
import zipfile
from typing import Callable, Iterable

from app.sessions.workspace import safe_join

# Bombs / runaway archives: cap count + total uncompressed bytes.
_MAX_FILES = 2000
_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB uncompressed
# Top-level names never written (workspace internals / packaging cruft).
_EXCLUDE_PARTS = {".git", "__MACOSX", ".DS_Store"}
_EXCLUDE_RELPATHS = {"SESSION.md"}


class ImportArchiveError(Exception):
    """Raised on an unsupported or unsafe archive."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _norm(name: str) -> str:
    """Normalize an archive member path to forward slashes, no leading slash."""
    return name.replace("\\", "/").lstrip("/")


def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    return rel in _EXCLUDE_RELPATHS or any(p in _EXCLUDE_PARTS for p in parts)


def _common_top(names: Iterable[str]) -> str | None:
    """
    If every entry sits under a single top-level directory (the usual `site/…`
    wrapper), return that prefix to strip so the site lands at the workspace root.
    """
    firsts: set[str] = set()
    for n in names:
        if "/" not in n:
            return None  # a root-level file → no single wrapper
        firsts.add(n.split("/", 1)[0])
    return firsts.pop() + "/" if len(firsts) == 1 else None


def _write_entries(
    workspace: str, entries: list[tuple[str, int, Callable[[], bytes]]]
) -> list[str]:
    """
    entries: (member_name, size, read_bytes). Strips a common top dir, filters
    unsafe/excluded paths, enforces caps, writes preserving structure. Returns the
    workspace-relative paths written.
    """
    names = [_norm(n) for n, _, _ in entries]
    strip = _common_top(names)
    written: list[str] = []
    total = 0

    for (raw_name, size, read), norm_name in zip(entries, names):
        rel = norm_name[len(strip):] if strip and norm_name.startswith(strip) else norm_name
        if not rel or rel.endswith("/"):
            continue
        if ".." in rel.split("/") or rel.startswith("/"):
            continue  # traversal — skip silently
        if _excluded(rel):
            continue
        try:
            abs_path = safe_join(workspace, rel)  # defence-in-depth vs traversal
        except ValueError:
            continue
        total += max(size, 0)
        if total > _MAX_TOTAL_BYTES:
            raise ImportArchiveError(413, "archive too large (over 200 MB uncompressed)")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as out:
            out.write(read())
        written.append(rel)
        if len(written) > _MAX_FILES:
            raise ImportArchiveError(413, f"archive has too many files (over {_MAX_FILES})")

    if not written:
        raise ImportArchiveError(400, "archive contained no usable files")
    return sorted(written)


def _guard_total(declared: int, running: list[int]) -> None:
    """Abort before decompressing if declared sizes already exceed the cap (bomb guard)."""
    running[0] += max(declared, 0)
    if running[0] > _MAX_TOTAL_BYTES:
        raise ImportArchiveError(413, "archive too large (over 200 MB uncompressed)")


def _zip_entries(path: str) -> list[tuple[str, int, Callable[[], bytes]]]:
    out: list[tuple[str, int, Callable[[], bytes]]] = []
    running = [0]
    with zipfile.ZipFile(path) as zf:
        for zi in zf.infolist():
            if zi.is_dir():
                continue
            # Skip symlinks (unix mode in the high bits of external_attr).
            if (zi.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            _guard_total(zi.file_size, running)  # check declared size before reading
            out.append((zi.filename, zi.file_size, (lambda zf=zf, zi=zi: zf.read(zi))))
        # Read eagerly while the ZipFile is open (the lambdas capture zf).
        return [(n, s, (lambda d=r(): d)) for (n, s, r) in out]


def _tar_entries(path: str) -> list[tuple[str, int, Callable[[], bytes]]]:
    out: list[tuple[str, int, Callable[[], bytes]]] = []
    running = [0]
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if not m.isfile():  # excludes dirs, symlinks, hardlinks, devices
                continue
            _guard_total(m.size, running)  # check declared size before extracting
            f = tf.extractfile(m)
            data = f.read() if f else b""
            out.append((m.name, m.size, (lambda d=data: d)))
    return out


def extract_archive(workspace: str, archive_path: str) -> list[str]:
    """
    Detect the archive type (by content, not just extension) and extract it into
    the workspace, preserving structure. Returns the relative paths written.
    """
    if zipfile.is_zipfile(archive_path):
        entries = _zip_entries(archive_path)
    elif tarfile.is_tarfile(archive_path):
        entries = _tar_entries(archive_path)
    else:
        raise ImportArchiveError(415, "unsupported archive — provide a .zip or .tar(.gz/.bz2/.xz)")
    return _write_entries(workspace, entries)
