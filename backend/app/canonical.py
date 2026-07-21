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
import hashlib
import logging
import os
import subprocess
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import approvals, evidence
from app.config import get_settings
from app.models import Approval, ContextSource, Evidence, Project
from app.project_context import (
    ManifestError,
    _resolve_under_root,
    _validate_rel,
    compute_revision,
    covers,
    detect_kind,
)
from app.redact import redact_text

_settings = get_settings()

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


def _read_current_text(abs_path: str, rel: str) -> str:
    old = ""
    if os.path.isfile(abs_path):
        try:
            with open(abs_path, encoding="utf-8") as f:
                old = f.read()
        except OSError as exc:
            raise CanonicalWriteError(f"cannot read current {rel}: {exc}") from None
        except UnicodeDecodeError:
            old = ""  # current file is binary — diff renders against empty
    return old


async def propose(
    db: AsyncSession,
    *,
    project: Project,
    rel_path: str,
    content: str | None = None,
    evidence_id: int | None = None,
    digest: str | None = None,
    producer: str,
    work_item_id: int | None = None,
) -> Approval:
    """Record a canonical-write proposal as a pending Approval row.

    Two payload forms, exactly one per call:
      v1 — inline `content` (prose/config-sized, `MAX_PROPOSAL_BYTES` cap);
      v2 — **by reference** to an evidence row (`evidence_id`): the content
      never transits the approval row — the payload pins the evidence's
      digest, re-verified at propose *and* apply time, so the inline byte cap
      stops being the promotion ceiling while tampering still fails closed.

    Never writes to the root. Inline content passes the secrets wall before it
    is persisted (the row is durable; evidence rules apply — by-reference
    content already passed it at capture). Flushes; caller commits.
    """
    if (content is None) == (evidence_id is None):
        raise CanonicalWriteError("exactly one of content or evidence_id is required")
    if not project.root_path:
        raise CanonicalWriteError("project has no context root bound")
    try:
        rel = _validate_rel(rel_path)
        abs_path = _resolve_under_root(project.root_path, rel)
    except ManifestError as exc:
        raise CanonicalWriteError(str(exc)) from None

    base_revision = compute_revision(project.root_path, rel, "file")

    if evidence_id is not None:
        row = await db.get(Evidence, evidence_id)
        if row is None or row.owner_id != project.owner_id \
                or row.project_id != project.id:
            raise CanonicalWriteError(
                f"evidence {evidence_id} not found in this project"
            )
        src = evidence.evidence_abs_path(row.project_id, row.rel_path)
        if src is None or not os.path.isfile(src):
            raise CanonicalWriteError(f"evidence {evidence_id} file is missing")
        size = os.path.getsize(src)
        if size > _settings.canonical_max_file_bytes:
            raise CanonicalWriteError(
                f"evidence is {size} bytes — canonical context is capped at "
                f"{_settings.canonical_max_file_bytes} bytes (canonical_max_file_bytes); "
                "promote the manifest or extracted files, not the package"
            )
        with open(src, "rb") as f:
            data = f.read()
        actual = hashlib.sha256(data).hexdigest()
        if row.digest and actual != row.digest:
            raise CanonicalWriteError(
                f"evidence {evidence_id} failed digest re-verification — "
                "stored file no longer matches its capture digest"
            )
        if digest and actual != digest:
            raise CanonicalWriteError(
                f"evidence {evidence_id} digest does not match the caller's pin"
            )
        try:
            new_text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            new_text = None
        diff = (
            _unified_diff(_read_current_text(abs_path, rel), new_text, rel)
            if new_text is not None
            else f"(binary evidence: {size} bytes, sha256 {actual})"
        )
        return await approvals.record_request(
            db,
            owner_id=project.owner_id,
            request_id=uuid.uuid4().hex,
            kind="canonical_write",
            producer=producer,
            project_id=project.id,
            work_item_id=work_item_id,
            payload={
                "v": 2,
                "rel_path": rel,
                "evidence_id": evidence_id,
                "digest": actual,
                "diff": diff,
                "base_revision": base_revision,
            },
        )

    content = redact_text(content)
    if len(content.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise CanonicalWriteError(
            f"proposal exceeds {MAX_PROPOSAL_BYTES} bytes — canonical context is "
            "prose/config-sized; large artifacts belong in evidence or outputs"
        )

    old = _read_current_text(abs_path, rel)
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


async def apply(
    db: AsyncSession,
    approval: Approval,
    project: Project,
    *,
    declare_source: bool = True,
) -> dict:
    """Apply an approved canonical write to the project root.

    Traversal-guarded write → git commit on git roots → source revision
    re-hash → **declaration** → decision evidence. Returns
    {"rel_path", "commit", "declared_source"} (commit None on non-git roots;
    `declared_source` None when nothing needed declaring). Flushes; caller
    commits the DB transaction.

    `declare_source` (P-0073): approving a promotion also declares the written
    path as a ContextSource when no existing source already covers it, so one
    decision both writes canon *and* makes it reach later sessions. Without
    this the two read as the same promise from the approver's chair and are
    not: projection is driven solely by declared sources, and promotion
    declared none.
    """
    payload = approval.payload or {}
    if not project.root_path:
        raise CanonicalWriteError("project has no context root bound")
    try:
        rel = _validate_rel(str(payload.get("rel_path") or ""))
        abs_path = _resolve_under_root(project.root_path, rel)
    except ManifestError as exc:
        raise CanonicalWriteError(str(exc)) from None

    if payload.get("evidence_id") is not None:
        # v2 by-reference: the bytes come from the evidence store at apply
        # time, re-verified against the digest pinned at propose time —
        # evidence altered between propose and approve fails closed.
        row = await db.get(Evidence, int(payload["evidence_id"]))
        src = (
            evidence.evidence_abs_path(row.project_id, row.rel_path)
            if row is not None else None
        )
        if src is None or not os.path.isfile(src):
            raise CanonicalWriteError(
                f"evidence {payload['evidence_id']} is no longer available"
            )
        with open(src, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != payload.get("digest"):
            raise CanonicalWriteError(
                f"evidence {payload['evidence_id']} failed digest re-verification "
                "at apply — refusing to write altered content to the canonical root"
            )
    else:
        data = str(payload.get("content") or "").encode("utf-8")

    try:
        os.makedirs(os.path.dirname(abs_path) or project.root_path, exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(data)
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
    existing = list(result.scalars().all())
    covered = False
    for source in existing:
        if covers(source.rel_path, rel):
            covered = True
            revision = compute_revision(project.root_path, source.rel_path, source.kind)
            if revision is not None:
                source.last_revision = revision

    # Declaration (P-0073): only when nothing already projects this path —
    # re-declaring a file that sits inside a declared directory would project
    # the same bytes twice and split its freshness across two rows.
    declared: dict | None = None
    declared_note = (
        "(already covered by a declared source)" if covered
        else "(declined by the approver)" if not declare_source
        else "(pending)"
    )
    if declare_source and not covered:
        orders = [s.bootstrap_order for s in existing if s.bootstrap_order is not None]
        source = ContextSource(
            owner_id=project.owner_id,
            project_id=project.id,
            kind=detect_kind(project.root_path, rel),
            rel_path=rel,
            # Appended after everything already declared: promoted canon becomes
            # an ordered bootstrap read without displacing the manifest's own
            # reading priority.
            bootstrap_order=(max(orders) + 1) if orders else 1,
            # The project's own sensitivity, not a fresh guess — promotion is not
            # the place to silently reclassify material.
            sensitivity="inherit",
        )
        source.last_revision = compute_revision(project.root_path, rel, source.kind)
        db.add(source)
        await db.flush()
        declared = {
            "id": source.id,
            "rel_path": source.rel_path,
            "kind": source.kind,
            "bootstrap_order": source.bootstrap_order,
        }
        declared_note = f"{source.rel_path} (order {source.bootstrap_order})"

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
            f"commit: {commit_sha or '(non-git root)'}\n"
            f"declared_source: {declared_note}\n\n"
            f"{payload.get('diff') or ''}"
        ),
        producer=approval.decided_by or "human",
    )
    await db.flush()
    return {"rel_path": rel, "commit": commit_sha, "declared_source": declared}
