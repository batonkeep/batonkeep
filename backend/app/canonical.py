"""
canonical.py — the approval baseline for canonical-context writes (S0).

The one policy rule the substrate ships: agents receive projected context
**read-only**; a write to a project's canonical root is only ever a
*proposal*. Proposing creates a pending `Approval` row (kind=canonical_write)
carrying the unified diff — nothing touches the root. On human approval the
engine applies the change (traversal-guarded), commits it on git roots,
re-hashes the touched source's revision, and captures the diff as
`decision` evidence. On denial nothing changes and the row records who said no.

No inheritance tree, no widening/narrowing calculus — that is pack-phase
policy. This module is deliberately one mechanism: propose → decide → apply.
"""
from __future__ import annotations

import difflib
import logging
import os
import subprocess
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import approvals, evidence
from app.models import Approval, ContextSource, Project
from app.project_context import ManifestError, _resolve_under_root, _validate_rel, compute_revision
from app.redact import redact_text

logger = logging.getLogger(__name__)

# Payload ceiling: proposals are prose/config-file sized, not media. Guards the
# Approval JSON column from multi-MB blobs.
MAX_PROPOSAL_BYTES = 512 * 1024

PAYLOAD_VERSION = 1  # additive-only evolution; readers tolerate unknown keys


class CanonicalWriteError(ValueError):
    """Caller-facing problems: bad path, oversized content, unbound root."""


def _unified_diff(old: str, new: str, rel_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


async def propose(
    db: AsyncSession,
    *,
    project: Project,
    rel_path: str,
    content: str,
    producer: str,
    work_item_id: int | None = None,
) -> Approval:
    """Record a canonical-write proposal as a pending Approval row.

    Never writes to the root. Content passes the secrets wall before it is
    persisted (the row is durable; evidence rules apply). Flushes; caller
    commits.
    """
    if not project.root_path:
        raise CanonicalWriteError("project has no context root bound")
    try:
        rel = _validate_rel(rel_path)
        abs_path = _resolve_under_root(project.root_path, rel)
    except ManifestError as exc:
        raise CanonicalWriteError(str(exc)) from None

    content = redact_text(content)
    if len(content.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise CanonicalWriteError(
            f"proposal exceeds {MAX_PROPOSAL_BYTES} bytes — canonical context is "
            "prose/config-sized; large artifacts belong in evidence or outputs"
        )

    old = ""
    if os.path.isfile(abs_path):
        try:
            with open(abs_path, encoding="utf-8") as f:
                old = f.read()
        except OSError as exc:
            raise CanonicalWriteError(f"cannot read current {rel}: {exc}") from None

    base_revision = compute_revision(project.root_path, rel, "file")
    return await approvals.record_request(
        db,
        owner_id=project.owner_id,
        request_id=uuid.uuid4().hex,
        kind="canonical_write",
        producer=producer,
        project_id=project.id,
        work_item_id=work_item_id,
        payload={
            "v": PAYLOAD_VERSION,
            "rel_path": rel,
            "content": content,
            "diff": _unified_diff(old, content, rel),
            "base_revision": base_revision,
        },
    )


async def apply(db: AsyncSession, approval: Approval, project: Project) -> dict:
    """Apply an approved canonical write to the project root.

    Traversal-guarded write → git commit on git roots → source revision
    re-hash → decision evidence. Returns {"rel_path", "commit"} (commit None
    on non-git roots). Flushes; caller commits the DB transaction.
    """
    payload = approval.payload or {}
    content = str(payload.get("content") or "")
    if not project.root_path:
        raise CanonicalWriteError("project has no context root bound")
    try:
        rel = _validate_rel(str(payload.get("rel_path") or ""))
        abs_path = _resolve_under_root(project.root_path, rel)
    except ManifestError as exc:
        raise CanonicalWriteError(str(exc)) from None

    try:
        os.makedirs(os.path.dirname(abs_path) or project.root_path, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        # Unwritable root (e.g. a root-owned host mount) — a recoverable operator
        # problem, not a crash.
        raise CanonicalWriteError(
            f"context root not writable by the backend: {rel}: {exc.strerror or exc}"
        ) from None

    commit_sha: str | None = None
    if os.path.isdir(os.path.join(project.root_path, ".git")):
        try:
            subprocess.run(
                ["git", "-C", project.root_path, "add", rel],
                capture_output=True, text=True, timeout=15, check=True,
            )
            subprocess.run(
                ["git", "-C", project.root_path,
                 # Committer identity inline — a host/container without a global
                 # git identity must not fail the approved write's commit.
                 "-c", "user.name=batonkeep", "-c", "user.email=noreply@batonkeep.local",
                 "commit",
                 "-m", f"canonical write: {rel} (approval {approval.request_id[:12]})",
                 "--author", "batonkeep <noreply@batonkeep.local>"],
                capture_output=True, text=True, timeout=15, check=True,
            )
            out = subprocess.run(
                ["git", "-C", project.root_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            commit_sha = out.stdout.strip()[:64] or None
        except (subprocess.SubprocessError, OSError) as exc:
            # The file IS written; a commit failure is surfaced, not silently
            # swallowed — the caller reports it alongside the applied write.
            logger.warning("canonical write applied but git commit failed: %s", exc)

    # Freshness: re-hash the touched source if it is a declared ContextSource
    # (or is inside one) so the next projection carries the new revision.
    result = await db.execute(
        select(ContextSource).where(ContextSource.project_id == project.id)
    )
    for source in result.scalars().all():
        if rel == source.rel_path or rel.startswith(source.rel_path + os.sep) \
                or source.rel_path == ".":
            revision = compute_revision(project.root_path, source.rel_path, source.kind)
            if revision is not None:
                source.last_revision = revision

    await evidence.capture_safe(
        db,
        owner_id=approval.owner_id,
        project_id=project.id,
        work_item_id=approval.work_item_id,
        kind="decision",
        filename=f"canonical_{os.path.basename(rel) or 'write'}.diff",
        text=(
            f"approved canonical write: {rel}\n"
            f"decided_by: {approval.decided_by or 'human'}\n"
            f"commit: {commit_sha or '(non-git root)'}\n\n"
            f"{payload.get('diff') or ''}"
        ),
        producer=approval.decided_by or "human",
    )
    await db.flush()
    return {"rel_path": rel, "commit": commit_sha}
