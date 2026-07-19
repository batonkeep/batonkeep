"""
sessions/packaging.py — immutable workspace package + artifact manifest (S0.5).

A completed workspace version becomes a durable, reproducible *artifact*: a zip
of the workspace tree at git HEAD with a `MANIFEST.json` (per-file sha256s,
commit sha, producer) at the zip root, captured as two append-only evidence
rows (kinds `package` + `manifest`). The package is the artifact, not the
harness — git internals, the session brief/ledger, provider convention files,
the projected `context/`, and package-manager dirs are all excluded (the
publish-bundle walk plus the projection-owned entries).

Packaging an uncommitted tree is refused: the engine owns the commit boundary,
and the manifest's `commit_sha` must actually describe the files it hashes.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile

from app.config import get_settings
from app.project_context import CONTEXT_DIRNAME
from app.sessions import publish
from app.sessions import workspace as ws
from app.work_ledger import LEDGER_FILENAME

_settings = get_settings()

MANIFEST_NAME = "MANIFEST.json"

# Projection-owned workspace entries, excluded on top of the publish walk's set
# (.git, SESSION.md, provider convention files, package/build dirs at any depth).
_PACKAGE_EXTRA_EXCLUDED = frozenset({CONTEXT_DIRNAME, LEDGER_FILENAME})


class PackagingError(ValueError):
    """A workspace state that can't be packaged (no commits / dirty tree)."""


class PackageTooLargeError(PackagingError):
    """The would-be package exceeds `package_max_bytes`."""


def _dirty_artifact_paths(porcelain: str) -> list[str]:
    """Paths from `git status --porcelain` that would actually enter the package.

    Harness files are legitimately dirty between turns — the session ledger
    rewrites SESSION.md *after* the turn commit, and the projection refreshes
    `context/`/WORKITEM.md per execution — so their dirtiness must not block
    packaging: they are excluded from the package anyway. Only uncommitted
    changes to *artifact* files make the manifest's commit_sha a lie."""
    excluded_top = publish._EXCLUDED_TOP | _PACKAGE_EXTRA_EXCLUDED
    dirty: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # Renames render as "old -> new"; the new path is what would package.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        parts = path.split("/")
        if parts[0] in excluded_top:
            continue
        if any(seg in publish._EXCLUDED_DIRS for seg in parts[:-1]):
            continue
        dirty.append(path)
    return dirty


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def package_name(session_id: str, commit_sha: str) -> str:
    """Stable evidence filename stem for (session × commit) — the idempotency key."""
    return f"{session_id[:8]}_{commit_sha[:8]}"


async def build_package(
    workspace: str, *, session_id: str, produced_by: str
) -> tuple[dict, bytes, str]:
    """Build (manifest, zip_bytes, commit_sha) for the workspace at git HEAD.

    Raises PackagingError when there is nothing committed or the tree is dirty,
    PackageTooLargeError when the summed file bytes exceed `package_max_bytes`.
    """
    commit = await ws.head_commit(workspace)
    if not commit:
        raise PackagingError("workspace has no committed version to package")
    rc, out = await ws._git_out(workspace, "status", "--porcelain")
    if rc != 0:
        raise PackagingError(f"git status failed in workspace (rc={rc})")
    dirty = _dirty_artifact_paths(out)
    if dirty:
        raise PackagingError(
            "workspace has uncommitted changes "
            f"({', '.join(dirty[:3])}{'…' if len(dirty) > 3 else ''}) — "
            "run/complete a turn first (the engine owns the commit boundary)"
        )

    root = os.path.abspath(workspace)
    files: list[dict] = []
    total = 0
    budget = _settings.package_max_bytes
    for rel in publish._publishable_files(
        root, extra_excluded_top=_PACKAGE_EXTRA_EXCLUDED
    ):
        full = ws.safe_join(root, rel)
        size = os.path.getsize(full)
        total += size
        if total > budget:
            raise PackageTooLargeError(
                f"package exceeds package_max_bytes ({budget} bytes) at {rel!r}"
            )
        files.append({"rel_path": rel, "sha256": _sha256_file(full), "bytes": size})

    manifest = {
        "v": 1,
        "commit_sha": commit,
        "produced_by": produced_by,
        "files": files,
        "file_count": len(files),
        "total_bytes": total,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        for entry in files:
            zf.write(ws.safe_join(root, entry["rel_path"]), arcname=entry["rel_path"])
    return manifest, buf.getvalue(), commit
