"""
project_context.py — S0 substrate: the context manifest, source revisions, and
the read-only projection an actor receives for a run/turn.

A Project may bind an external context root (`Project.root_path`) whose
manifest (`batonkeep.yaml`) declares canonical sources — ordered bootstrap
reads plus domain-labelled directories. The DB records *where truth lives and
its last-seen revision* (ContextSource), never the content: the root stays the
single truth store.

Projection is how an execution actually receives that context: the selected
sources are materialized read-only under `<workdir>/context/`, the working
ledger (WORKITEM.md) is rendered from structured fields, and a ContextReceipt
— paths + hashes only, content-safe by construction — is persisted *before*
the executor starts, so the receipt survives a crashed run. Sensitivity/budget
cuts are recorded as receipt exclusions, surfaced not silent.

Freshness (`last_revision`/`last_checked_at`) is a signal for humans; nothing
here auto-rewrites a stale source. Secrets are never projected — they live in
the credential store (existing boundary) and no code path here reads it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.evidence import evidence_abs_path
from app.models import ContextReceipt, ContextSource, Evidence, Project, WorkItem
from app.version import APP_VERSION
from app.work_ledger import (
    LEDGER_FILENAME,
    format_evidence_line,
    render_ledger,
    sha256_text,
)

logger = logging.getLogger(__name__)
_settings = get_settings()

PROJECTION_VERSION = "proj-v3"  # v3: P-0073 undeclared-content coverage warning
CONTEXT_DIRNAME = "context"
# Where a work item's pinned evidence is materialized inside the projected
# context dir: <workdir>/context/evidence/<evidence_id>_<basename>.
EVIDENCE_DIRNAME = "evidence"
DEFAULT_MANIFEST_REL = "batonkeep.yaml"
MANIFEST_API_VERSION = "batonkeep.dev/v1alpha1"
MANIFEST_KIND = "Project"

# Workspace paths the projection owns. In git-versioned session workspaces they
# are appended to .gitignore so per-turn commits never track projected context.
_IGNORE_ENTRIES = (f"{CONTEXT_DIRNAME}/", LEDGER_FILENAME)


class ManifestError(ValueError):
    """The manifest is missing, unreadable, or structurally invalid."""


@dataclass
class ProjectManifest:
    """Parsed `batonkeep.yaml`. Unknown keys warn (recorded here), never fail."""

    bootstrap: list[str] = field(default_factory=list)
    domains: dict[str, str] = field(default_factory=dict)  # label → rel dir
    evidence_dir: str | None = None
    warnings: list[str] = field(default_factory=list)


# ── Path safety ───────────────────────────────────────────────────────────────

def _validate_rel(rel: str) -> str:
    """Normalize a manifest-relative path; reject absolute paths and traversal.
    Relative paths keep exports portable across machines."""
    rel = str(rel).strip()
    if os.path.isabs(rel) or rel.startswith("~"):
        raise ManifestError(f"absolute paths are not allowed in the manifest: {rel!r}")
    rel = rel.strip("/")  # tolerate a trailing slash on dir entries
    if not rel:
        rel = "."
    norm = os.path.normpath(rel)
    if norm == ".." or norm.startswith(".." + os.sep):
        raise ManifestError(f"path escapes the project root: {rel!r}")
    return norm


def _resolve_under_root(root: str, rel: str) -> str:
    """Traversal-safe join (the run-assets/workspace pattern): the resolved path
    must stay under the project root."""
    base = os.path.realpath(root)
    resolved = os.path.realpath(os.path.join(base, rel))
    if not (resolved == base or resolved.startswith(base + os.sep)):
        raise ManifestError(f"source path escapes the project root: {rel!r}")
    return resolved


# ── Manifest parsing ──────────────────────────────────────────────────────────

_KNOWN_TOP_KEYS = {"apiVersion", "kind", "name", "description", "context"}
_KNOWN_CONTEXT_KEYS = {"bootstrap", "domains", "evidence"}


def parse_manifest_text(text: str) -> ProjectManifest:
    """Parse + validate manifest YAML. Stdlib + PyYAML only; unknown keys warn."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a YAML mapping")

    api = raw.get("apiVersion")
    if api != MANIFEST_API_VERSION:
        raise ManifestError(
            f"unsupported apiVersion {api!r} (expected {MANIFEST_API_VERSION!r})"
        )
    kind = raw.get("kind")
    if kind != MANIFEST_KIND:
        raise ManifestError(f"unsupported kind {kind!r} (expected {MANIFEST_KIND!r})")

    manifest = ProjectManifest()
    for key in raw:
        if key not in _KNOWN_TOP_KEYS:
            manifest.warnings.append(f"unknown key {key!r} ignored")

    context = raw.get("context") or {}
    if not isinstance(context, dict):
        raise ManifestError("`context` must be a mapping")
    for key in context:
        if key not in _KNOWN_CONTEXT_KEYS:
            manifest.warnings.append(f"unknown key context.{key!r} ignored")

    bootstrap = context.get("bootstrap") or []
    if not isinstance(bootstrap, list):
        raise ManifestError("`context.bootstrap` must be a list of relative paths")
    seen: set[str] = set()
    for entry in bootstrap:
        if not isinstance(entry, str):
            raise ManifestError(f"`context.bootstrap` entries must be strings: {entry!r}")
        rel = _validate_rel(entry)
        if rel in seen:
            manifest.warnings.append(f"duplicate bootstrap entry {rel!r} ignored")
            continue
        seen.add(rel)
        manifest.bootstrap.append(rel)

    domains = context.get("domains") or {}
    if not isinstance(domains, dict):
        raise ManifestError("`context.domains` must be a mapping of label → dir")
    for label, rel in domains.items():
        if not isinstance(rel, str):
            raise ManifestError(f"`context.domains.{label}` must be a relative path")
        manifest.domains[str(label)[:64]] = _validate_rel(rel)

    evidence = context.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, str):
            raise ManifestError("`context.evidence` must be a relative path")
        manifest.evidence_dir = _validate_rel(evidence)

    return manifest


def load_manifest(project: Project) -> ProjectManifest:
    """Read + parse the project's manifest from its bound root."""
    if not project.root_path:
        raise ManifestError("project has no root_path bound")
    rel = _validate_rel(project.manifest_rel or DEFAULT_MANIFEST_REL)
    path = _resolve_under_root(project.root_path, rel)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {rel}: {exc}") from None
    return parse_manifest_text(text)


# ── Revision hashing ──────────────────────────────────────────────────────────

def _iter_files(base: str):
    """Yield (rel_path, abs_path) for every regular file under base, sorted,
    excluding .git internals — one traversal shared by sizing + hashing + copy."""
    if os.path.isfile(base):
        yield os.path.basename(base), base
        return
    collected: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            if os.path.isfile(abs_path):
                collected.append((os.path.relpath(abs_path, base), abs_path))
    yield from sorted(collected)


def covers(source_rel: str, file_rel: str) -> bool:
    """Does a declared source at `source_rel` contain `file_rel`? A source of
    "." is the whole root. Shared by projection-coverage and by the canonical
    apply path, so "already reachable" means the same thing in both."""
    return (
        source_rel == "."
        or file_rel == source_rel
        or file_rel.startswith(source_rel + os.sep)
    )


@dataclass
class Coverage:
    """What the canonical root holds that no declared source covers — the
    P-0073 warning. `count` is a floor when `truncated` (the scan is bounded);
    `sample` is illustrative, never the full list."""

    count: int = 0
    sample: list[str] = field(default_factory=list)
    truncated: bool = False


COVERAGE_SAMPLE_MAX = 10


def scan_undeclared(project: Project, sources: list[ContextSource]) -> Coverage:
    """Files under the project's context root that no *declared* source covers.

    This is the seam P-0073 exists for: "approved into canon" and "projected
    into sessions" read as one promise but are two systems, and a projection
    that is a strict subset of the root is otherwise silent. Measured against
    *declared* sources, not projected ones, so a source cut for budget/missing
    keeps its own exclusion reason instead of being double-reported here.

    Bounded and best-effort: a walk error or the file cap ends the scan rather
    than failing a projection, since this only ever produces a warning.
    """
    if not project.root_path or not os.path.isdir(project.root_path):
        return Coverage()
    rels = [s.rel_path for s in sources]
    if any(r == "." for r in rels):
        return Coverage()  # the whole root is declared — nothing to scan

    # The manifest describes the context; it is not context. Same for the
    # evidence dir, which is the evidence store's own tree.
    skip = {DEFAULT_MANIFEST_REL}
    try:
        manifest = load_manifest(project)
    except ManifestError:
        manifest = None
    if manifest is not None and manifest.evidence_dir:
        rels = [*rels, manifest.evidence_dir]

    cov = Coverage()
    scanned = 0
    cap = _settings.context_coverage_scan_max_files
    root = project.root_path
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            base = os.path.relpath(dirpath, root)
            base = "" if base == "." else base
            # Prune: a declared directory needs no walking (it is covered
            # wholesale), and .git is never context. Pruning is what keeps the
            # scan proportional to the *undeclared* part of the root.
            dirnames[:] = sorted(
                d for d in dirnames
                if d != ".git"
                and not any(covers(r, os.path.join(base, d)) for r in rels)
            )
            for name in sorted(filenames):
                file_rel = os.path.join(base, name)
                if not os.path.isfile(os.path.join(dirpath, name)):
                    continue
                scanned += 1
                if scanned > cap:
                    cov.truncated = True
                    return cov
                if file_rel in skip or any(covers(r, file_rel) for r in rels):
                    continue
                cov.count += 1
                if len(cov.sample) < COVERAGE_SAMPLE_MAX:
                    cov.sample.append(file_rel)
    except OSError as exc:
        logger.warning("coverage scan of %s stopped: %s", root, exc)
        cov.truncated = True
    return cov


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_kind(root: str, rel: str) -> str:
    """git (a checkout — has .git) · dir · file. Missing paths default to file;
    the refresh path records them as unhashable rather than failing."""
    try:
        path = _resolve_under_root(root, rel)
    except ManifestError:
        return "file"
    if os.path.isdir(path):
        return "git" if os.path.isdir(os.path.join(path, ".git")) else "dir"
    return "file"


def compute_revision(root: str, rel: str, kind: str) -> str | None:
    """The source's current revision: git HEAD sha (kind=git) or a sha256 —
    of the file content, or a merkle of sorted per-file sha256s for dirs.
    None when the path is missing/unreadable (freshness surfaced, not faked)."""
    try:
        path = _resolve_under_root(root, rel)
    except ManifestError:
        return None
    if not os.path.exists(path):
        return None

    if kind == "git":
        try:
            out = subprocess.run(
                ["git", "-C", path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return out.stdout.strip()[:64] or None
        except (subprocess.SubprocessError, OSError):
            return None

    if os.path.isfile(path):
        try:
            return _sha256_file(path)
        except OSError:
            return None

    # dir merkle: hash the sorted (rel_path, file_sha) pairs.
    h = hashlib.sha256()
    try:
        for rel_f, abs_f in _iter_files(path):
            h.update(f"{rel_f}:{_sha256_file(abs_f)}\n".encode())
    except OSError:
        return None
    return h.hexdigest()


def _source_bytes(root: str, rel: str) -> int:
    """Total content bytes a source would project (0 if missing)."""
    try:
        path = _resolve_under_root(root, rel)
    except ManifestError:
        return 0
    if not os.path.exists(path):
        return 0
    total = 0
    for _rel_f, abs_f in _iter_files(path):
        try:
            total += os.path.getsize(abs_f)
        except OSError:
            continue
    return total


# ── Source sync + refresh ─────────────────────────────────────────────────────

async def sync_sources_from_manifest(
    db: AsyncSession, project: Project
) -> tuple[list[ContextSource], list[str]]:
    """Import/refresh ContextSource rows from the project's manifest (idempotent
    upsert on rel_path). Manually-declared sources not in the manifest are left
    alone. Does not commit — the route owns the transaction."""
    manifest = load_manifest(project)

    result = await db.execute(
        select(ContextSource).where(ContextSource.project_id == project.id)
    )
    by_rel = {s.rel_path: s for s in result.scalars().all()}

    declared: dict[str, dict] = {}
    for i, rel in enumerate(manifest.bootstrap):
        declared[rel] = {"bootstrap_order": i + 1, "domain": None}
    for label, rel in manifest.domains.items():
        entry = declared.setdefault(rel, {"bootstrap_order": None, "domain": None})
        entry["domain"] = label

    touched: list[ContextSource] = []
    root = project.root_path or ""
    for rel, attrs in declared.items():
        source = by_rel.get(rel)
        kind = detect_kind(root, rel)
        if source is None:
            source = ContextSource(
                owner_id=project.owner_id,
                project_id=project.id,
                kind=kind,
                rel_path=rel,
                bootstrap_order=attrs["bootstrap_order"],
                domain=attrs["domain"],
            )
            db.add(source)
        else:
            source.kind = kind
            source.bootstrap_order = attrs["bootstrap_order"]
            source.domain = attrs["domain"]
        touched.append(source)
    await db.flush()
    return touched, manifest.warnings


async def refresh_sources(db: AsyncSession, project: Project) -> list[ContextSource]:
    """Re-hash every declared source; update freshness. A missing source keeps
    its last_revision but gets a fresh last_checked_at (staleness is a signal
    for humans — nothing auto-fixes it). Does not commit."""
    result = await db.execute(
        select(ContextSource)
        .where(ContextSource.project_id == project.id)
        .order_by(ContextSource.id)
    )
    sources = list(result.scalars().all())
    now = datetime.now(UTC)
    for source in sources:
        if project.root_path:
            revision = compute_revision(project.root_path, source.rel_path, source.kind)
            if revision is not None:
                source.last_revision = revision
        source.last_checked_at = now
    await db.flush()
    return sources


# ── Projection ────────────────────────────────────────────────────────────────

def _clear_context_dir(ctx_dir: str) -> None:
    """Remove a previous projection. Files are read-only (0444) but their dirs
    stay writable, so plain rmtree unlinks them fine."""
    if os.path.isdir(ctx_dir):
        shutil.rmtree(ctx_dir, ignore_errors=True)


def _chmod_read_only(base: str) -> None:
    for _rel_f, abs_f in _iter_files(base):
        try:
            os.chmod(abs_f, 0o444)
        except OSError:
            pass


def _materialize(src_path: str, dest_path: str) -> None:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.isdir(src_path):
        shutil.copytree(
            src_path, dest_path, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git"),
        )
    else:
        shutil.copy2(src_path, dest_path)


def _ensure_gitignored(workdir: str) -> None:
    """In a git-versioned workspace (build sessions), keep projected context and
    the ledger out of per-turn commits. Idempotent append; non-git dirs skip."""
    if not os.path.isdir(os.path.join(workdir, ".git")):
        return
    gi_path = os.path.join(workdir, ".gitignore")
    try:
        existing = ""
        if os.path.isfile(gi_path):
            with open(gi_path, encoding="utf-8") as f:
                existing = f.read()
        lines = {ln.strip() for ln in existing.splitlines()}
        missing = [e for e in _IGNORE_ENTRIES if e not in lines]
        if missing:
            with open(gi_path, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(missing) + "\n")
    except OSError as exc:  # best-effort: never block a turn on gitignore upkeep
        logger.warning("could not update workspace .gitignore: %s", exc)


async def project_for_execution(
    db: AsyncSession,
    *,
    owner_id: str,
    project_id: str | None,
    work_item_id: int | None = None,
    workdir: str,
    run_id: int | None = None,
    session_turn_id: int | None = None,
    changed_files: list[str] | None = None,
) -> ContextReceipt | None:
    """Project the declared context into the execution workspace and persist the
    receipt — **before the executor starts**, so the receipt survives a crash.

    1. Resolve Project (+ WorkItem if attached).
    2. Select sources: bootstrap order, then manifest/declaration order.
    3. Materialize read-only under `<workdir>/context/` (budget/missing cuts
       recorded as exclusions — surfaced, never silent).
    4. Render the deterministic working ledger → WORKITEM.md → ledger_sha.
    5. Persist the ContextReceipt (paths + hashes only). Commits.

    Returns None when the execution carries no project (legacy rows) — nothing
    to project, no receipt to fake.
    """
    if project_id is None:
        return None
    project = await db.get(Project, project_id)
    if project is None or project.owner_id != owner_id:
        logger.warning("projection skipped: project %s not found for owner", project_id)
        return None
    work_item: WorkItem | None = None
    if work_item_id is not None:
        work_item = await db.get(WorkItem, work_item_id)
        if work_item is not None and work_item.owner_id != owner_id:
            work_item = None

    result = await db.execute(
        select(ContextSource)
        .where(ContextSource.project_id == project.id)
        .order_by(
            ContextSource.bootstrap_order.is_(None),
            ContextSource.bootstrap_order,
            ContextSource.id,
        )
    )
    sources = list(result.scalars().all())

    ctx_dir = os.path.join(workdir, CONTEXT_DIRNAME)
    _clear_context_dir(ctx_dir)

    projected: list[dict] = []
    exclusions: list[dict] = []
    total_bytes = 0
    budget = _settings.context_projection_max_bytes
    now = datetime.now(UTC)

    if project.root_path:
        for source in sources:
            try:
                src_path = _resolve_under_root(project.root_path, source.rel_path)
            except ManifestError:
                exclusions.append({"rel_path": source.rel_path, "reason": "unsafe-path"})
                continue
            if not os.path.exists(src_path):
                exclusions.append({"rel_path": source.rel_path, "reason": "missing"})
                source.last_checked_at = now
                continue

            size = _source_bytes(project.root_path, source.rel_path)
            if total_bytes + size > budget:
                exclusions.append({"rel_path": source.rel_path, "reason": "budget"})
                continue

            revision = compute_revision(project.root_path, source.rel_path, source.kind)
            source.last_revision = revision or source.last_revision
            source.last_checked_at = now

            dest = os.path.normpath(os.path.join(ctx_dir, source.rel_path))
            try:
                _materialize(src_path, dest)
            except OSError as exc:
                exclusions.append({"rel_path": source.rel_path, "reason": f"copy-failed: {exc}"})
                continue
            total_bytes += size
            projected.append(
                {"source_id": source.id, "rel_path": source.rel_path, "revision": revision}
            )
        if projected:
            _chmod_read_only(ctx_dir)
    elif sources:
        exclusions = [{"rel_path": s.rel_path, "reason": "no-root"} for s in sources]

    # P-0073: a projection that is a strict subset of the canonical root is
    # otherwise silent — the pilot #43 failure mode, where an actor briefed to
    # read approved canon received a projection that simply lacked it and went
    # looking for the files on disk. Recorded against the root itself so the
    # two required keys stay present for any consumer, with the count/sample
    # carrying the detail.
    coverage = scan_undeclared(project, sources)
    if coverage.count:
        exclusions.append({
            "rel_path": ".",
            "reason": "undeclared",
            "count": coverage.count,
            "sample": coverage.sample,
            "truncated": coverage.truncated,
        })

    # Evidence index for the ledger: paths + digests only (never content) —
    # PROJECT-WIDE, not just the bound work item. The whole point of durable
    # evidence is that a *fresh* work item's operator can see its predecessors'
    # outputs; filtering to the bound item left cold handoffs blind. Newest rows
    # win under the cap (the full index stays queryable via the API); rendering
    # order is (work item, id) so the ledger reads chronologically per item.
    ev_result = await db.execute(
        select(Evidence)
        .where(Evidence.project_id == project.id)
        .order_by(Evidence.id.desc())
        .limit(_settings.evidence_index_max_rows)
    )
    ev_rows = sorted(
        ev_result.scalars().all(),
        key=lambda e: (e.work_item_id is None, e.work_item_id or 0, e.id),
    )
    evidence_index = [
        {
            "evidence_id": e.id,
            "work_item_id": e.work_item_id,
            "kind": e.kind,
            "rel_path": e.rel_path,
            "digest": e.digest,
        }
        for e in ev_rows
    ]
    index_sha = sha256_text("\n".join(format_evidence_line(e) for e in evidence_index))

    # Materialize the bound work item's *pinned* evidence read-only under
    # context/evidence/ — its own byte budget (packages are bigger than text
    # sources), digest re-verified at copy so silently-altered evidence fails
    # closed as an exclusion rather than propagating.
    materialized: list[dict] = []
    ev_exclusions: list[dict] = []
    pins = ((work_item.pinned_evidence or {}).get("items", [])
            if work_item is not None else [])
    if pins:
        ev_dir = os.path.join(ctx_dir, EVIDENCE_DIRNAME)
        ev_budget = _settings.context_evidence_max_bytes
        ev_total = 0
        for pin in pins:
            eid = pin.get("evidence_id") if isinstance(pin, dict) else None
            row = await db.get(Evidence, eid) if isinstance(eid, int) else None
            if row is None or row.project_id != project.id:
                ev_exclusions.append({"evidence_id": eid, "reason": "missing"})
                continue
            src = evidence_abs_path(row.project_id, row.rel_path)
            if src is None or not os.path.isfile(src):
                ev_exclusions.append({"evidence_id": eid, "reason": "missing"})
                continue
            size = os.path.getsize(src)
            if ev_total + size > ev_budget:
                ev_exclusions.append({"evidence_id": eid, "reason": "budget"})
                continue
            if row.digest and _sha256_file(src) != row.digest:
                ev_exclusions.append({"evidence_id": eid, "reason": "digest-mismatch"})
                logger.warning(
                    "pinned evidence %s failed digest re-verification — excluded", eid
                )
                continue
            rel = os.path.join(
                CONTEXT_DIRNAME, EVIDENCE_DIRNAME,
                f"{row.id}_{os.path.basename(row.rel_path)}",
            )
            dest = os.path.join(ev_dir, f"{row.id}_{os.path.basename(row.rel_path)}")
            try:
                _materialize(src, dest)
                os.chmod(dest, 0o444)
            except OSError as exc:
                ev_exclusions.append(
                    {"evidence_id": eid, "reason": f"copy-failed: {exc}"}
                )
                continue
            ev_total += size
            total_bytes += size
            materialized.append(
                {"evidence_id": row.id, "rel_path": rel, "digest": row.digest}
            )

    ledger_text = render_ledger(
        project_name=project.name,
        work_item=work_item,
        changed_files=changed_files or [],
        evidence_index=evidence_index,
        pinned_inputs=[m["rel_path"] for m in materialized],
        undeclared_count=coverage.count,
    )
    ledger_path = os.path.join(workdir, LEDGER_FILENAME)
    try:
        if os.path.exists(ledger_path):
            os.unlink(ledger_path)  # previous turn's copy is read-only
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write(ledger_text)
        os.chmod(ledger_path, 0o444)
    except OSError as exc:
        logger.warning("could not write %s: %s", LEDGER_FILENAME, exc)

    _ensure_gitignored(workdir)

    receipt = ContextReceipt(
        owner_id=owner_id,
        project_id=project.id,
        work_item_id=work_item.id if work_item is not None else None,
        run_id=run_id,
        session_turn_id=session_turn_id,
        projection_version=PROJECTION_VERSION,
        sources=projected,
        ledger_sha=sha256_text(ledger_text),
        exclusions=exclusions or None,
        evidence={
            "v": 1,
            "index_count": len(evidence_index),
            "index_sha": index_sha,
            "materialized": materialized,
            "exclusions": ev_exclusions,
        },
        approx_bytes=total_bytes + len(ledger_text.encode("utf-8")),
        # Provenance stamps: harness now; cli_version is filled in when a CLI
        # candidate actually starts (the orchestrators own that).
        harness_version=APP_VERSION[:32],
    )
    db.add(receipt)
    await db.commit()
    return receipt
