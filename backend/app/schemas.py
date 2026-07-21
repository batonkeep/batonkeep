"""
schemas.py — Pydantic request/response schemas for the REST API (§10).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# P-0046 code-exec execution policies (single source of truth: code_exec.POLICIES).
from app.providers.tools.code_exec import POLICIES as EXEC_POLICIES

# P-0056/D-0052: bounds for the per-task run-timeout override (seconds). Floor 1 min;
# ceiling 6h (runaway-cost protection). None means "inherit the global default".
TIMEOUT_SECONDS_MIN = 60
TIMEOUT_SECONDS_MAX = 6 * 60 * 60  # 21600


def _validate_timeout_seconds(v: int | None) -> int | None:
    """Validate a per-task run-timeout override (P-0056/D-0052). None = global default."""
    if v is None:
        return None
    if v < TIMEOUT_SECONDS_MIN or v > TIMEOUT_SECONDS_MAX:
        raise ValueError(
            f"timeout_seconds must be between {TIMEOUT_SECONDS_MIN} and "
            f"{TIMEOUT_SECONDS_MAX} (6h), or null for the default"
        )
    return v


def _validate_image_model_id(v: str | None, *, allow_empty: bool = False) -> str | None:
    """Validate an image-gen model override against the catalog (P-0046 slice 6).
    `allow_empty` permits the "" sentinel (PATCH: clear back to provider default)."""
    if v is None:
        return None
    if allow_empty and v == "":
        return v
    from app.providers.image_models import get_image_model

    if get_image_model(v) is None:
        raise ValueError(f"unknown image_model_id: {v!r}")
    return v


class ImageModelOut(BaseModel):
    """A selectable image-generation model (P-0046 slice 6). `available` reflects
    whether the model's home provider currently has a usable credential."""

    id: str
    label: str
    provider: str
    model: str
    cost_per_image: float
    cost_per_mtok: float
    available: bool

# ── Routing policy (§4.3) ────────────────────────────────────────────────────

class RoutingPolicy(BaseModel):
    strategy: str = "capability"  # capability | fixed | round_robin | cost_optimized
    candidates: list[str] = ["mock"]
    capability_tags: list[str] = []
    failover: bool = True
    overflow_to: str | None = None
    max_attempts: int = 3


# ── Task ─────────────────────────────────────────────────────────────────────

# ── Projects (S0 substrate) ─────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    # Free-form label (general | infra | research | …); engine never branches on it.
    kind: str = "general"
    sensitivity: str = "normal"
    root_path: str | None = None
    # S0.4: ask the server to create a managed context root under the data volume
    # (projects/<id>/context, git-init'd with a starter manifest) instead of
    # naming a pre-existing directory. Mutually exclusive with root_path.
    create_root: bool = False
    description: str | None = None

    @field_validator("sensitivity")
    @classmethod
    def _valid_sensitivity(cls, v: str) -> str:
        if v not in {"normal", "confidential"}:
            raise ValueError("sensitivity must be 'normal' or 'confidential'")
        return v

    @model_validator(mode="after")
    def _root_choice(self) -> ProjectCreate:
        if self.create_root and self.root_path:
            raise ValueError("create_root and root_path are mutually exclusive")
        return self


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    name: str
    kind: str
    status: str
    sensitivity: str
    is_default: bool
    root_path: str | None
    manifest_rel: str | None
    description: str | None
    # P-0078: the project's planner default (null → fall back to the first available
    # instance). What would *actually* run is GET /api/projects/{id}/planner, which
    # applies the sovereignty fence on top of this.
    planner_provider: str | None = None
    planner_model: str | None = None
    created_at: datetime
    updated_at: datetime


class PlannerSettingsIn(BaseModel):
    """Set (or clear) a project's planner default. Both null → fall back to the
    first available instance, which is the default posture: a planner is never a
    mandatory per-project decision."""

    provider: str | None = None
    model: str | None = None


class PlannerSettingsOut(BaseModel):
    """A project's planner selection: what is *stored*, and what would actually run.
    The two differ when the stored provider is absent (fallback) or when the
    sovereignty fence pins a confidential project's planner to a local model — so the
    UI shows the operator the truth instead of re-deriving the fence client-side."""

    provider: str | None
    model: str | None
    effective_provider: str | None
    effective_model: str | None
    local_pinned: bool
    # Why the effective selection differs from the stored one — or, when nothing can
    # run, why not. Null when the stored selection is exactly what would run.
    note: str | None = None


# Valid WorkItem state machine (S0): validated on PATCH; closed_at is stamped on
# done/dropped and cleared on reopen. Free transitions would let a client silently
# resurrect closed work without the reopen event being visible in history.
#
# `proposed` (P-0078 slice 2) is the planner's entry state: the planner mints work
# items (decompose / triage) but never approves durable intent, so its items land
# here awaiting the operator's accept (→ open) or reject (→ dropped). Nothing
# transitions *into* it — only the planner creates one — so an operator can never
# push confirmed work back into the proposal queue.
WORK_ITEM_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"open", "dropped"},
    "open": {"in_progress", "blocked", "done", "dropped"},
    "in_progress": {"open", "awaiting_approval", "blocked", "done", "dropped"},
    "awaiting_approval": {"in_progress", "blocked", "done", "dropped"},
    "blocked": {"open", "in_progress", "dropped"},
    "done": {"reopened"},
    "dropped": {"reopened"},
    "reopened": {"in_progress", "blocked", "done", "dropped"},
}

_WORK_ITEM_RISKS = {"low", "medium", "high"}


class WorkItemCreate(BaseModel):
    # Free taxonomy (task | incident | change | investigation | review | chore | …);
    # engine code never branches on it.
    kind: str = "task"
    title: str
    # Durable intent — what "done" means, independent of any transcript.
    objective: str = ""
    next_action: str | None = None
    risk: str = "low"
    parent_id: int | None = None
    # Content-safe origin reference: {source, kind, external_id, ts}.
    signal: dict[str, Any] | None = None

    @field_validator("risk")
    @classmethod
    def _valid_risk(cls, v: str) -> str:
        if v not in _WORK_ITEM_RISKS:
            raise ValueError(f"risk must be one of {sorted(_WORK_ITEM_RISKS)}")
        return v

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        return v[:256]


class WorkItemPatch(BaseModel):
    title: str | None = None
    objective: str | None = None
    # The single honest next step — agents propose it, the orchestrator writes it.
    next_action: str | None = None
    state: str | None = None
    risk: str | None = None
    kind: str | None = None
    # Appends {ts, actor, text} to the append-only decisions list.
    add_decision: str | None = None
    decision_actor: str = "human"
    # Replaces the work item's pinned-evidence *inputs* (curated, small — the
    # projection materializes these into new workspaces). List of evidence ids;
    # None = untouched, [] = clear all pins.
    pinned_evidence: list[int] | None = None

    @field_validator("risk")
    @classmethod
    def _valid_risk(cls, v: str | None) -> str | None:
        if v is not None and v not in _WORK_ITEM_RISKS:
            raise ValueError(f"risk must be one of {sorted(_WORK_ITEM_RISKS)}")
        return v

    @field_validator("state")
    @classmethod
    def _valid_state(cls, v: str | None) -> str | None:
        if v is not None and v not in WORK_ITEM_TRANSITIONS:
            raise ValueError(f"unknown state {v!r}")
        return v


class SubtaskItemIn(BaseModel):
    """One checklist item on the way in. `id` present → edit an existing item;
    absent → mint a new one. `expected` (path/glob) makes it verifiable."""

    id: str | None = None
    label: str
    expected: str | None = None
    status: str | None = None      # set-only: proposed | confirmed | dropped
    done: bool | None = None       # set-only: mark an asserted item done


class SubtaskProposeIn(BaseModel):
    """Append agent/operator-proposed items (status=proposed)."""

    items: list[SubtaskItemIn]
    proposed_by: str = "operator"


class SubtaskSetIn(BaseModel):
    """Authoritative confirm/modify: the operator's full desired checklist."""

    items: list[SubtaskItemIn]
    actor: str = "operator"


class WorkItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    project_id: str
    kind: str
    state: str
    title: str
    objective: str
    next_action: str | None
    risk: str
    parent_id: int | None
    signal: dict[str, Any] | None
    decisions: list[Any] | None
    pinned_evidence: dict[str, Any] | None
    # P-0069 B2: the sub-task checklist (raw) + its grounded progress roll-up.
    subtasks: dict[str, Any] | None = None
    subtask_progress: dict[str, int] | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None

    @model_validator(mode="after")
    def _fill_progress(self):
        from app import subtasks as _st
        # Only compute when there's a checklist (keeps legacy work items lean).
        object.__setattr__(
            self, "subtask_progress",
            _st.progress(self.subtasks) if self.subtasks else None,
        )
        return self


class ContextSourceDeclare(BaseModel):
    """POST /api/projects/{id}/context-sources body. rel_path=None imports every
    source the project manifest declares; an explicit rel_path declares one."""

    rel_path: str | None = None
    # git | dir | file; None → auto-detected from the filesystem.
    kind: str | None = None
    bootstrap_order: int | None = None
    domain: str | None = None
    sensitivity: str = "inherit"

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str | None) -> str | None:
        if v is not None and v not in {"git", "dir", "file"}:
            raise ValueError("kind must be git, dir, or file")
        return v

    @field_validator("sensitivity")
    @classmethod
    def _valid_sensitivity(cls, v: str) -> str:
        if v not in {"inherit", "normal", "confidential"}:
            raise ValueError("sensitivity must be inherit, normal, or confidential")
        return v


class ContextSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    project_id: str
    kind: str
    rel_path: str
    bootstrap_order: int | None
    domain: str | None
    sensitivity: str
    last_revision: str | None
    last_checked_at: datetime | None


class ContextSourcesOut(BaseModel):
    """Declare/import result: the touched sources + manifest warnings (unknown
    keys etc. — surfaced, never silently dropped)."""

    sources: list[ContextSourceOut]
    warnings: list[str] = []


class ContextReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    project_id: str
    work_item_id: int | None
    run_id: int | None
    session_turn_id: int | None
    projection_version: str
    sources: list[Any] | None
    ledger_sha: str | None
    exclusions: list[Any] | None
    approx_bytes: int
    # Provenance stamps: distinguish model regressions from CLI/harness ones.
    harness_version: str | None = None
    cli_version: str | None = None
    created_at: datetime


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    project_id: str
    work_item_id: int | None
    run_id: int | None
    session_turn_id: int | None
    kind: str
    rel_path: str
    digest: str | None
    producer: str
    bytes: int
    sensitivity: str
    created_at: datetime


class PackageIn(BaseModel):
    """Optional knobs for the session workspace-package capture (S0.5)."""

    work_item_id: int | None = None
    # "Hand this artifact to that work item": append the captured package to the
    # target work item's pinned-evidence inputs in the same transaction, so the
    # next operator's workspace materializes it. Also applies when the package
    # already existed (idempotent capture, pin still lands).
    pin_to_work_item_id: int | None = None


class PackageOut(BaseModel):
    """Result of packaging a session workspace: the two evidence rows
    (`package` zip + standalone `manifest`) and whether they pre-existed
    (idempotent per session × commit)."""

    package: EvidenceOut
    manifest: EvidenceOut | None
    existing: bool


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    request_id: str
    kind: str
    status: str
    project_id: str | None
    work_item_id: int | None
    session_id: str | None
    run_id: int | None
    payload: dict[str, Any] | None
    producer: str
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None


class CanonicalProposeIn(BaseModel):
    """A proposed write to a project's canonical context root. Never applied
    directly — it becomes a pending approval carrying the diff. Exactly one of
    `content` (inline, prose-sized) or `evidence_id` (by-reference promotion:
    the bytes stay in the evidence store, digest-pinned at propose and
    re-verified at apply)."""

    rel_path: str
    content: str | None = None
    evidence_id: int | None = None
    # Optional caller pin for by-reference proposals; must match the stored
    # evidence digest when given.
    digest: str | None = None
    producer: str = "human"
    work_item_id: int | None = None


class ApprovalDecideIn(BaseModel):
    approved: bool


class ApprovalDecideOut(BaseModel):
    approval: ApprovalOut
    # canonical_write + approved: what was applied ({"rel_path", "commit"}).
    applied: dict[str, Any] | None = None


class TaskCreate(BaseModel):
    # S0 substrate: the Project this task belongs to. None = the owner's default
    # ("Personal workspace") — existing clients keep working unchanged.
    project_id: str | None = None
    # Optional WorkItem this task serves; must belong to the resolved project.
    work_item_id: int | None = None
    name: str
    description: str | None = None
    category: str | None = None
    prompt_template: str = ""
    params: dict[str, Any] | None = None
    schedule_kind: str = "none"
    schedule_expr: str | None = None
    timezone: str = "UTC"  # IANA tz for cron interpretation
    want_markdown: bool = True
    want_json: bool = False
    enabled: bool = True
    routing: RoutingPolicy | None = None
    # P-0046 code-exec execution policy: off | confirmation | allow-safe | auto.
    # Unattended tasks must set allow-safe/auto explicitly to use code-exec.
    exec_policy: str = "confirmation"
    # P-0046 slice 6 follow-up: image-gen model override (catalog id; cross-provider
    # allowed). None = inherit the text provider's default image model.
    image_model_id: str | None = None
    # P-0050: per-task generated-asset retention caps. None = unlimited (default).
    asset_max_count: int | None = None
    asset_max_bytes: int | None = None
    # P-0056/D-0052: per-task run timeout in seconds. None = global default (1800s).
    # Bounds elapsed wall-clock time; clamped to [60s, 6h].
    timeout_seconds: int | None = None

    @field_validator("exec_policy")
    @classmethod
    def _valid_policy(cls, v: str) -> str:
        if v not in EXEC_POLICIES:
            raise ValueError(f"exec_policy must be one of {sorted(EXEC_POLICIES)}")
        return v

    @field_validator("image_model_id")
    @classmethod
    def _valid_image_model(cls, v: str | None) -> str | None:
        return _validate_image_model_id(v)

    @field_validator("asset_max_count", "asset_max_bytes")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("retention cap must be >= 0 (None = unlimited)")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _valid_timeout(cls, v: int | None) -> int | None:
        return _validate_timeout_seconds(v)


class TaskUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    prompt_template: str | None = None
    params: dict[str, Any] | None = None
    schedule_kind: str | None = None
    schedule_expr: str | None = None
    timezone: str | None = None
    want_markdown: bool | None = None
    want_json: bool | None = None
    enabled: bool | None = None
    routing: RoutingPolicy | None = None
    exec_policy: str | None = None  # P-0046; validated below
    # P-0046 slice 6 follow-up: image-gen model override. "" clears to default.
    image_model_id: str | None = None
    # P-0050 retention caps. -1 clears back to unlimited (None means "unchanged").
    asset_max_count: int | None = None
    asset_max_bytes: int | None = None
    # P-0056/D-0052 per-task run timeout (seconds). -1 clears back to the global
    # default; None means "unchanged" (exclude_unset). Bounds elapsed wall-clock time.
    timeout_seconds: int | None = None

    @field_validator("exec_policy")
    @classmethod
    def _valid_policy(cls, v: str | None) -> str | None:
        if v is not None and v not in EXEC_POLICIES:
            raise ValueError(f"exec_policy must be one of {sorted(EXEC_POLICIES)}")
        return v

    @field_validator("image_model_id")
    @classmethod
    def _valid_image_model(cls, v: str | None) -> str | None:
        return _validate_image_model_id(v, allow_empty=True)

    @field_validator("asset_max_count", "asset_max_bytes")
    @classmethod
    def _retention_range(cls, v: int | None) -> int | None:
        if v is not None and v < -1:
            raise ValueError("retention cap must be >= 0, or -1 to clear to unlimited")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_range(cls, v: int | None) -> int | None:
        # -1 is the "clear back to default" sentinel (handled in the route); otherwise
        # the same [60s, 6h] bound as create applies.
        if v == -1:
            return v
        return _validate_timeout_seconds(v)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    project_id: str | None = None  # S0 substrate; always set on new creates
    work_item_id: int | None = None  # S0 substrate; wired in a later slice
    name: str
    description: str | None
    category: str | None
    prompt_template: str
    params: dict[str, Any] | None
    schedule_kind: str
    schedule_expr: str | None
    timezone: str
    want_markdown: bool
    want_json: bool
    enabled: bool
    routing: dict[str, Any] | None
    exec_policy: str = "confirmation"  # P-0046 code-exec execution policy
    image_model_id: str | None = None  # P-0046 slice 6: image-gen model override
    asset_max_count: int | None = None  # P-0050 retention cap; None = unlimited
    asset_max_bytes: int | None = None  # P-0050 retention cap; None = unlimited
    timeout_seconds: int | None = None  # P-0056/D-0052; None = global default (1800s)
    created_at: datetime
    updated_at: datetime


# ── Run assets (P-0050) ─────────────────────────────────────────────────────────

class RunAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    rel_path: str
    mime: str | None
    bytes: int
    created_at: datetime


class StorageUsageOut(BaseModel):
    """Stored run-asset usage for an owner (optionally one task) — P-0050."""

    count: int
    bytes: int


# ── Run ───────────────────────────────────────────────────────────────────────

class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    task_id: int
    project_id: str | None = None  # S0 substrate; inherited from the task at enqueue
    work_item_id: int | None = None  # S0 substrate; wired in a later slice
    trigger: str
    status: str
    summary: str | None
    error: str | None
    provider: str | None
    model: str | None
    tier: str | None
    attempts: list[Any] | None
    overflow_used: bool
    deferred_until: datetime | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    subagents: int
    tool_calls: int
    markdown_path: str | None
    json_path: str | None
    output_flags: dict | None = None  # P-0069: outputs_missing advisory (NULL = clean)
    # Run assets (P-0050) are fetched via GET /api/runs/{id}/assets, not inlined here
    # — keeps the base RunOut free of an eager-load on every list/broadcast.
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


# ── RunEvent ──────────────────────────────────────────────────────────────────

class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    seq: int
    ts: datetime
    kind: str
    phase: str | None
    message: str | None
    data: dict[str, Any] | None


# ── Build sessions (M1.1) ───────────────────────────────────────────────────

class SessionCreate(BaseModel):
    # S0 substrate: the Project this session belongs to. None = the owner's default.
    project_id: str | None = None
    # Optional WorkItem this session serves; must belong to the resolved project.
    work_item_id: int | None = None
    title: str | None = None
    goal: str | None = None
    # initial provider instance id (e.g. "grok", "agy", "mock")
    provider: str | None = None
    # optional task-type template id (D-0011): seeds goal + guidance in SESSION.md
    template: str | None = None
    # P-0009 #1: pin this session to a local model (confidential — never off-box).
    confidential: bool = False
    # P-0049: per-session model override for the chosen API provider. None = the
    # provider's catalog preferred.default. CLI plans own their model elsewhere.
    model: str | None = None
    # P-0046 slice 6 follow-up: image-gen model override (catalog id; cross-provider
    # allowed). None = inherit the text provider's default image model.
    image_model_id: str | None = None

    @field_validator("image_model_id")
    @classmethod
    def _valid_image_model(cls, v: str | None) -> str | None:
        return _validate_image_model_id(v)


class SessionTemplateOut(BaseModel):
    """A session task type (P-0010 / D-0011) the UI offers as a starter card."""

    id: str
    label: str
    description: str


class TaskTemplateOut(BaseModel):
    """A starter task preset the UI offers on a fresh install.

    `input` is a TaskCreate-shaped payload (seeded enabled=False) the form pre-fills;
    nothing is persisted until the user reviews and saves it.
    """

    id: str
    label: str
    description: str
    input: TaskCreate


class SessionUpdate(BaseModel):
    # rename a session (other fields like provider are switched via turns)
    title: str | None = None
    # toggle the P-0009 #1 confidential (local-only) pin
    confidential: bool | None = None
    # P-0046 code-exec execution policy: off | confirmation | allow-safe | auto
    exec_policy: str | None = None
    # P-0049: per-session model override (API path). Sentinel "" clears it back to the
    # provider's catalog default; a model id sets it; None leaves it unchanged.
    model: str | None = None
    # P-0046 slice 6 follow-up: image-gen model override. Sentinel "" clears it back
    # to the provider default; a catalog id sets it; None leaves it unchanged.
    image_model_id: str | None = None
    # Optional per-session spend cap (USD, API path). A positive value sets the cap;
    # 0 clears it back to no cap (also the "raise budget" action's path); None leaves
    # it unchanged. Negative is rejected.
    budget_usd: float | None = None

    @field_validator("budget_usd")
    @classmethod
    def _valid_budget(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("budget_usd must be >= 0 (0 clears the cap)")
        return v

    @field_validator("exec_policy")
    @classmethod
    def _valid_policy(cls, v: str | None) -> str | None:
        if v is not None and v not in EXEC_POLICIES:
            raise ValueError(f"exec_policy must be one of {sorted(EXEC_POLICIES)}")
        return v

    @field_validator("image_model_id")
    @classmethod
    def _valid_image_model(cls, v: str | None) -> str | None:
        return _validate_image_model_id(v, allow_empty=True)


class ApprovalDecision(BaseModel):
    # P-0046 slice 3b: operator's verdict on a pending code-exec confirmation.
    approved: bool


class TurnCreate(BaseModel):
    message: str
    # optional provider switch for this and subsequent turns
    provider: str | None = None
    # optional model switch for this and subsequent turns (P-0049, API path). "" clears
    # back to the provider default; a model id pins it; None leaves it unchanged.
    model: str | None = None


class CaptureRequest(BaseModel):
    """Capture the web-TTY terminal lane's workspace edits as a version + artifact
    turn (D-0017 thread 2). `instance` labels the turn with the CLI that drove it."""

    instance: str | None = None


class SummaryOut(BaseModel):
    """The session ledger's auto-maintained summary (D-0017 thread 1). `summary` is
    null when none has been produced (e.g. summarization disabled / skipped)."""

    summary: str | None = None


class FileChangeOut(BaseModel):
    """One file a turn produced (D-0017 thread 2). status ∈ added/changed/removed;
    additions/deletions are None for binary files."""

    path: str
    status: str
    additions: int | None = None
    deletions: int | None = None


class SessionTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    seq: int
    provider: str | None
    prompt: str
    response: str | None
    status: str
    error: str | None
    # M1.3 versioning: the workspace commit this turn produced (if any) + summary.
    commit_sha: str | None = None
    diffstat: str | None = None
    # D-0017 thread 2: the per-file artifacts this turn produced (the headline
    # result). Stored as a JSON string on the model; parsed to a list here.
    changed_files: list[FileChangeOut] | None = None
    # P-0069 item 6: free-default output flag — {"v":1,"unbacked":[...]} or None.
    # Referenced files the response linked to that aren't in the committed tree.
    output_flags: dict | None = None
    # Per-turn token/cost usage (API path) so the UI can show + sum session spend.
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    created_at: datetime
    finished_at: datetime | None

    @field_validator("changed_files", mode="before")
    @classmethod
    def _parse_changed_files(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return None
        return v


class PlanRequestIn(BaseModel):
    """Request a planning turn (P-0078). All fields optional: an operator note to
    steer it, and a per-call provider/model override (else the project's planner
    default → first available; a confidential project is pinned local regardless)."""

    message: str | None = None
    provider: str | None = None
    model: str | None = None


class PlannerRunOut(BaseModel):
    """A planning-turn record (P-0078) — the audit + spend trail for the planner."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    work_item_id: int | None
    status: str
    provider: str | None
    model: str | None
    local_pinned: bool
    # The exact prompt the planner was given. Surfaced because "it proposed nothing"
    # is un-diagnosable without it — the usual cause is that the turn was told very
    # little, not that the model misbehaved. Content-safe by construction (paths,
    # counts and durable intent; never source content or raw evidence).
    request: str | None
    response: str | None
    error: str | None
    proposals: dict | None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    created_at: datetime
    finished_at: datetime | None


class FileEntryOut(BaseModel):
    """One workspace file in the session file browser (P-0016 b)."""

    path: str
    size: int
    modified: float


class VersionOut(BaseModel):
    """One workspace version (commit) — the Undo/History list entry (M1.3)."""

    commit: str
    short: str
    ts: str
    message: str


class VersionDiffOut(BaseModel):
    """The diff a single version introduced (M1.3) + its per-file list (D-0017)."""

    commit: str
    diffstat: str
    diff: str
    files: list[FileChangeOut] = []


class RestoreRequest(BaseModel):
    # the version (commit sha) to restore the workspace to
    commit: str


class RestoreOut(BaseModel):
    commit: str
    message: str
    restored_from: str


class UploadOut(BaseModel):
    """Result of dropping files into a session (M1.5). Paths are workspace-relative
    so the user/agent can reference them by name in the conversation."""

    paths: list[str]
    # the workspace commit the upload produced (a version), if anything changed
    commit_sha: str | None = None


class ImportOut(BaseModel):
    """Result of importing an existing site (zip/tar/git) into a session workspace."""

    paths: list[str]
    count: int
    commit_sha: str | None = None


class GitImportIn(BaseModel):
    """Import a site by cloning a public https git URL."""

    url: str
    branch: str | None = None


class PublishOut(BaseModel):
    """Publish/share state of a session's build (M1.4)."""

    published: bool
    # public share token + relative URL path; None when revoked/never published
    share_token: str | None = None
    share_path: str | None = None  # e.g. "/api/share/<token>/"
    # the workspace commit snapshotted into the live bundle
    version: str | None = None
    kind: str = "site"
    file_count: int | None = None
    updated_at: datetime | None = None


class CloudflareConfigIn(BaseModel):
    """Set the owner-level Cloudflare credentials (D-0009 host connector)."""

    api_token: str       # high-privilege deploy token; encrypted at rest, never in a sandbox
    account_id: str


class CloudflareStatusOut(BaseModel):
    """Connector state for the UI — never reveals the token."""

    configured: bool
    account_id: str | None = None


class CloudflareDeployIn(BaseModel):
    """Per-deploy options. Project is per-session; omit to use the session's default."""

    project_name: str | None = None


class CloudflareDeployOut(BaseModel):
    """Result of a Cloudflare Pages deploy."""

    url: str
    project: str


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    project_id: str | None = None  # S0 substrate; always set on new creates
    work_item_id: int | None = None  # S0 substrate; wired in a later slice
    title: str
    provider: str | None
    workspace_path: str
    preview_token: str
    status: str
    cf_project: str | None = None  # Cloudflare Pages project this session deploys to
    confidential: bool = False  # P-0009 #1: pinned to a local model
    model: str | None = None  # P-0049: per-session model override (API path)
    exec_policy: str = "confirmation"  # P-0046 code-exec execution policy
    image_model_id: str | None = None  # P-0046 slice 6: image-gen model override
    # Optional per-session spend cap (USD, API path). None = no cap (opt-in).
    budget_usd: float | None = None
    # Cumulative session spend (sum of succeeded turns), for the live cost surface.
    # Populated by the detail/list endpoints; defaults to 0 elsewhere.
    cost_usd: float = 0.0
    # Content signals for the UI (e.g. delete-confirmation strength). Populated by
    # the list endpoint; default to empty/false elsewhere.
    turn_count: int = 0
    published: bool = False
    created_at: datetime
    updated_at: datetime


# ── Provider health (§4.4) ────────────────────────────────────────────────────

class ProviderHealth(BaseModel):
    name: str          # instance id ("claude" or "claude:work")
    template: str      # provider template ("claude") — UI groups instances under this
    label: str         # human display label for the account
    model: str | None = None  # active model for API instances (None for CLI)
    kind: str
    tier: str
    healthy: bool
    # Operator suspend toggle (default True). Disabled providers stay listed but are
    # skipped in routing and reported unhealthy — suspend without deleting auth.
    enabled: bool = True
    cooldown_until: datetime | None
    last_reset_seen: datetime | None
    est_used_pct: float | None
    usage_seen_at: datetime | None = None  # when /usage quota was last captured (D-0023 b)
    mode: str  # plan | api | open | mock
    capability_tags: list[str] = []  # effective routing tags (override > template) — P-0044
    # Effective $/Mtok the instance meters at (override > known-model book > template).
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0
    # Prompt-cache $/Mtok (cache-read cheap, cache-write a premium). Explicit per-model
    # rate if known, else derived from the input rate. Surfaced so the UI shows the
    # full cost picture once caching is on.
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0
    pricing_source: str = "template"  # "override" | "registry" | "template"


class ProviderEnabledUpdate(BaseModel):
    """Operator suspend/reactivate toggle for a provider instance."""

    enabled: bool


class ProviderLimitsUpdate(BaseModel):
    """Operator-declared limit windows (§4.4)."""
    window_seconds: int
    window_limit: int  # estimated cap within the window


class ProviderModelUpdate(BaseModel):
    """Set (or clear, when null/empty) an instance's runtime model override.

    Optional per-Mtok pricing: when provided, stored as a per-instance override so
    cost estimates track the chosen model. Omit both to leave pricing resolution to
    the known-model price book (keyed by the effective model id). Set
    `clear_pricing` to drop a previously-set override and fall back to the book.
    """
    model: str | None = None
    cost_in_per_mtok: float | None = None
    cost_out_per_mtok: float | None = None
    clear_pricing: bool = False


class ModelPricingOut(BaseModel):
    """Known-model price lookup (GET /api/model-pricing). `known` is False when the
    backend doesn't recognise the model — the UI then asks the operator to enter rates."""
    model: str
    known: bool
    cost_in_per_mtok: float | None = None
    cost_out_per_mtok: float | None = None
    # Prompt-cache rates for the model (explicit if pinned, else derived from input).
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None


class CatalogModelOut(BaseModel):
    """One model in a provider's catalog (P-0049), with resolved pricing + usage."""
    id: str
    enabled: bool
    capabilities: list[str] = []
    known: bool                       # recognised by the price book
    cost_in_per_mtok: float | None = None
    cost_out_per_mtok: float | None = None
    use_count: int = 0                # owner's runs on this model id
    last_used: str | None = None      # ISO8601 of the most recent run


class ProviderCatalogOut(BaseModel):
    """A provider's model catalog (GET /api/providers/{template}/catalog). Models are
    sorted most-used then most-recently-used; `effective_model` is what a run resolves
    to today, `preferred` is the per-capability preferred map."""
    template: str
    models: list[CatalogModelOut]
    preferred: dict[str, str] = {}
    effective_model: str | None = None
    capabilities_vocab: list[str] = []


class CatalogModelUpdate(BaseModel):
    """Add/update one catalog model's structure + optional pricing (write-through to
    the flat overlay). Omit a field to leave it unchanged."""
    id: str
    enabled: bool | None = None
    capabilities: list[str] | None = None
    cost_in_per_mtok: float | None = None
    cost_out_per_mtok: float | None = None
    clear_pricing: bool = False


class CatalogPreferredUpdate(BaseModel):
    """Set (or clear, when model is null/empty) a provider's preferred model for a
    capability (`default`/`coding`/`synthesis`/`longcontext`/`image`/`vision`/`realtime`)."""
    capability: str
    model: str | None = None


class ConsoleConfig(BaseModel):
    """Whether the scoped in-UI console is available (does not reveal the token)."""
    available: bool


class LoginRequest(BaseModel):
    """App-level login (D-0023). Single-operator password check; when TOTP is
    enrolled (D-0056) a valid 6-digit code must accompany the password."""
    password: str
    totp_code: str | None = None


class AuthStatus(BaseModel):
    """Whether app-auth is enabled and whether the caller is authenticated.
    ``totp_enabled`` tells the login page to show the code field (D-0056)."""
    auth_enabled: bool
    authenticated: bool
    totp_enabled: bool = False


class TotpStatus(BaseModel):
    """Settings → Security panel state (D-0056)."""
    enabled: bool          # enrolled + activated → login requires a code
    pending: bool          # secret generated but not yet confirmed with a live code
    break_glass: bool      # TOTP_DISABLED=1 is set — second factor skipped


class TotpSetupOut(BaseModel):
    """Enrollment material — shown once; the QR is rendered client-side."""
    secret: str            # base32, for manual key entry
    otpauth_uri: str       # for the QR code


class TotpCode(BaseModel):
    """A 6-digit code from the authenticator app."""
    code: str


# ── Stats ─────────────────────────────────────────────────────────────────────

class StatsOut(BaseModel):
    runs_today: int
    success_rate: float
    avg_duration_ms: float | None
    runs_by_provider: dict[str, int]
    failover_rate: float
    deferred_now: int
    cost_today_usd: float
    active_runs: int


# ── Credentials (BYO-key) ─────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    provider: str  # template name or instance id ("openai-api" / "openai-api:team")
    api_key: str  # plaintext; encrypted before storage
    label: str | None = None


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    provider: str
    label: str | None = None
    key_hint: str | None = None  # non-secret last-4 ("…wxyz"), never the full key
    created_at: datetime
    last_used_at: datetime | None = None


class UsageSummaryOut(BaseModel):
    """Owner spend surface (P-0009 #2) — today / 7d / by-provider + budget cap."""

    spend_today_usd: float
    spend_7d_usd: float
    by_provider_today: dict[str, float]
    daily_budget_usd: float           # 0 = unlimited
    remaining_today_usd: float | None = None  # None when unlimited
    over_budget: bool


# ── Operational cockpit (D-0022 Task A — audience A, local-only) ──────────────

class CockpitLatencyOut(BaseModel):
    avg_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    sample: int = 0


class CockpitRunsOut(BaseModel):
    total: int
    by_status: dict[str, int]
    by_provider: dict[str, int]
    by_trigger: dict[str, int]
    success_rate: float
    error_rate: float
    deferred_now: int
    active_runs: int


class CockpitReliabilityOut(BaseModel):
    failover_rate: float
    failover_reasons: dict[str, int]
    retried_runs: int
    budget_degraded_runs: int


class CockpitActivityOut(BaseModel):
    sessions_total: int
    sessions_active: int
    sessions_archived: int
    sessions_confidential: int
    turns_total: int
    turns_by_status: dict[str, int]


class CockpitOut(BaseModel):
    """
    The consolidated operator cockpit (D-0022 audience A): the user's view of their
    own work. Local-only and sovereign by construction — nothing here is shared.
    """

    window_days: int
    since: datetime
    generated_at: datetime
    spend: UsageSummaryOut
    runs: CockpitRunsOut
    latency: CockpitLatencyOut
    reliability: CockpitReliabilityOut
    errors_by_class: dict[str, int]
    activity: CockpitActivityOut


class SecretStatusOut(BaseModel):
    """One row of the named secrets-management surface (P-0009 #3)."""

    provider: str
    tier: str
    # "openai_compatible" | "anthropic" | "gemini" — drives whether a model is settable
    kind: str = ""
    env_key: str | None = None
    local: bool = False
    source: str  # "stored" | "env" | "missing"
    key_hint: str | None = None
    model: str | None = None  # effective model id (override > instance > template default)
    last_used_at: datetime | None = None


# ── Mode ─────────────────────────────────────────────────────────────────────

class ModeOut(BaseModel):
    mode: str  # plan | byo_key | hosted
    deployment_mode: str
    plan_cli_allowed: bool


# ── Custom providers (D-0026) ─────────────────────────────────────────────────

class CustomProviderCreate(BaseModel):
    """Create a new custom local/open-API provider endpoint."""
    id: str                       # slug — unique, lowercase alphanum + hyphens
    label: str                    # display name (e.g. "My Ollama")
    base_url: str                 # endpoint (e.g. "http://localhost:11434/v1")
    default_model: str            # e.g. "gemma4:12b"
    auth_type: str = "none"       # none | bearer | api_key_header
    env_key: str | None = None    # optional env var to resolve the API key from
    local: bool = False           # True → eligible for confidential (P-0009 #1) routing
    extra_models: str = ""        # comma-separated extra model names for display
    capability_tags: list[str] = []  # routing tags; empty → sensible auto default (P-0044)
    cost_in_per_mtok: float = 0.0    # $/Mtok input; 0 = unknown (falls back to price book)
    cost_out_per_mtok: float = 0.0   # $/Mtok output


class CustomProviderUpdate(BaseModel):
    """Update fields of an existing custom provider. All fields optional."""
    label: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    auth_type: str | None = None
    env_key: str | None = None
    local: bool | None = None
    enabled: bool | None = None
    extra_models: str | None = None
    capability_tags: list[str] | None = None
    cost_in_per_mtok: float | None = None
    cost_out_per_mtok: float | None = None


class ProviderTagsUpdate(BaseModel):
    """Set (or clear, when empty) a built-in provider's routing-tag override (P-0044)."""
    capability_tags: list[str] = []


class CustomProviderOut(BaseModel):
    """Custom provider as returned by the API."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    base_url: str
    default_model: str
    auth_type: str
    env_key: str | None = None
    local: bool
    enabled: bool
    extra_models: str
    capability_tags: list[str] = []
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0
