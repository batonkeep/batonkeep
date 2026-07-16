"""
evidence.py — the append-only evidence store (S0 substrate).

Evidence is the attributable record of what happened: reports, diffs, logs,
verifications, decisions. Files live under `<evidence_dir>/project_<id>/` and
are indexed by the `Evidence` table with a content digest. The store is
append-only by construction: capture + read only — no update path exists
anywhere (route or module), and retention deletion is the only removal
(deferred to the retention pass; nothing here deletes).

Two hard rules at capture time:

- **Secrets wall:** all *text* evidence is routed through `redact_text` before
  it touches disk. Evidence is append-only, so a leaked credential here would
  be permanent — this is the redaction requirement the sanitizer module's
  contract names. Binary payloads are stored as-is (digests would break
  otherwise); binary producers are the existing asset pipeline, which carries
  generated media, not echoed terminal text.
- **Digest-at-capture:** the sha256 is computed from the exact bytes written,
  so export/restore and audits can re-verify content integrity later.

Producers call `capture()` inside their own transaction (it flushes, the
caller commits) or `capture_safe()` from best-effort hooks that must never
break a run/turn.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Evidence
from app.redact import redact_text

logger = logging.getLogger(__name__)
_settings = get_settings()

EVIDENCE_KINDS = ("report", "diff", "log", "verification", "decision", "asset-ref")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME = 128


def _safe_name(name: str) -> str:
    """Flatten a filename to a safe basename (mirrors the asset-name pattern)."""
    base = os.path.basename(name or "").strip() or "evidence.txt"
    base = _SAFE_NAME_RE.sub("_", base)[:_MAX_NAME].strip("._") or "evidence.txt"
    return base


def evidence_abs_path(project_id: str, rel_path: str) -> str | None:
    """Resolve an evidence row's absolute path under the store root, refusing
    traversal (the run-assets serving pattern). rel_path is stored relative to
    the store root (`project_<id>/<file>`) so exports stay portable."""
    base = os.path.realpath(os.path.join(_settings.evidence_dir, f"project_{project_id}"))
    target = os.path.realpath(os.path.join(_settings.evidence_dir, rel_path))
    if not (target == base or target.startswith(base + os.sep)):
        return None
    return target


async def capture(
    db: AsyncSession,
    *,
    owner_id: str,
    project_id: str,
    kind: str,
    filename: str,
    text: str | None = None,
    data: bytes | None = None,
    producer: str = "system",
    work_item_id: int | None = None,
    run_id: int | None = None,
    session_turn_id: int | None = None,
    sensitivity: str = "inherit",
) -> Evidence:
    """Write one evidence file + its index row. Exactly one of text/data.

    Text passes the secrets wall (redact_text) before hitting disk; the digest
    is of the bytes actually written. Flushes; the caller owns the commit.
    """
    if (text is None) == (data is None):
        raise ValueError("capture() takes exactly one of text= or data=")
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")

    payload = redact_text(text).encode("utf-8") if text is not None else data
    assert payload is not None

    name = f"{uuid.uuid4().hex[:12]}_{_safe_name(filename)}"
    rel_path = os.path.join(f"project_{project_id}", name)
    abs_path = os.path.join(_settings.evidence_dir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        f.write(payload)

    row = Evidence(
        owner_id=owner_id,
        project_id=project_id,
        work_item_id=work_item_id,
        run_id=run_id,
        session_turn_id=session_turn_id,
        kind=kind,
        rel_path=rel_path,
        digest=hashlib.sha256(payload).hexdigest(),
        producer=producer[:96],
        bytes=len(payload),
        sensitivity=sensitivity,
    )
    db.add(row)
    await db.flush()
    return row


async def capture_safe(db: AsyncSession, **kwargs) -> Evidence | None:
    """Best-effort capture for producer hooks: evidence must never break the
    run/turn that produced it. Logs and returns None on any failure."""
    try:
        return await capture(db, **kwargs)
    except Exception:
        logger.exception("evidence capture failed (kind=%s)", kwargs.get("kind"))
        return None
