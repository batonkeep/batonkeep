"""
schemas.py — Pydantic request/response schemas for the REST API (§10).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


# ── Routing policy (§4.3) ────────────────────────────────────────────────────

class RoutingPolicy(BaseModel):
    strategy: str = "capability"  # capability | fixed | round_robin | cost_optimized
    candidates: list[str] = ["mock"]
    capability_tags: list[str] = []
    failover: bool = True
    overflow_to: Optional[str] = None
    max_attempts: int = 3


# ── Task ─────────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    prompt_template: str = ""
    params: Optional[dict[str, Any]] = None
    schedule_kind: str = "none"
    schedule_expr: Optional[str] = None
    timezone: str = "UTC"  # IANA tz for cron interpretation
    want_markdown: bool = True
    want_json: bool = False
    enabled: bool = True
    routing: Optional[RoutingPolicy] = None


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    prompt_template: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    schedule_kind: Optional[str] = None
    schedule_expr: Optional[str] = None
    timezone: Optional[str] = None
    want_markdown: Optional[bool] = None
    want_json: Optional[bool] = None
    enabled: Optional[bool] = None
    routing: Optional[RoutingPolicy] = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    name: str
    description: Optional[str]
    category: Optional[str]
    prompt_template: str
    params: Optional[dict[str, Any]]
    schedule_kind: str
    schedule_expr: Optional[str]
    timezone: str
    want_markdown: bool
    want_json: bool
    enabled: bool
    routing: Optional[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


# ── Run ───────────────────────────────────────────────────────────────────────

class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    task_id: int
    trigger: str
    status: str
    summary: Optional[str]
    error: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    tier: Optional[str]
    attempts: Optional[list[Any]]
    overflow_used: bool
    deferred_until: Optional[datetime]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    subagents: int
    tool_calls: int
    markdown_path: Optional[str]
    json_path: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    duration_ms: Optional[int]


# ── RunEvent ──────────────────────────────────────────────────────────────────

class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    seq: int
    ts: datetime
    kind: str
    phase: Optional[str]
    message: Optional[str]
    data: Optional[dict[str, Any]]


# ── Build sessions (M1.1) ───────────────────────────────────────────────────

class SessionCreate(BaseModel):
    title: Optional[str] = None
    goal: Optional[str] = None
    # initial provider instance id (e.g. "grok", "agy", "mock")
    provider: Optional[str] = None
    # optional task-type template id (D-0011): seeds goal + guidance in SESSION.md
    template: Optional[str] = None
    # P-0009 #1: pin this session to a local model (confidential — never off-box).
    confidential: bool = False


class SessionTemplateOut(BaseModel):
    """A session task type (P-0010 / D-0011) the UI offers as a starter card."""

    id: str
    label: str
    description: str


class SessionUpdate(BaseModel):
    # rename a session (other fields like provider are switched via turns)
    title: Optional[str] = None
    # toggle the P-0009 #1 confidential (local-only) pin
    confidential: Optional[bool] = None


class TurnCreate(BaseModel):
    message: str
    # optional provider switch for this and subsequent turns
    provider: Optional[str] = None


class SessionTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    seq: int
    provider: Optional[str]
    prompt: str
    response: Optional[str]
    status: str
    error: Optional[str]
    # M1.3 versioning: the workspace commit this turn produced (if any) + summary.
    commit_sha: Optional[str] = None
    diffstat: Optional[str] = None
    created_at: datetime
    finished_at: Optional[datetime]


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
    """The diff a single version introduced (M1.3)."""

    commit: str
    diffstat: str
    diff: str


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
    commit_sha: Optional[str] = None


class ImportOut(BaseModel):
    """Result of importing an existing site (zip/tar/git) into a session workspace."""

    paths: list[str]
    count: int
    commit_sha: Optional[str] = None


class GitImportIn(BaseModel):
    """Import a site by cloning a public https git URL."""

    url: str
    branch: Optional[str] = None


class PublishOut(BaseModel):
    """Publish/share state of a session's build (M1.4)."""

    published: bool
    # public share token + relative URL path; None when revoked/never published
    share_token: Optional[str] = None
    share_path: Optional[str] = None  # e.g. "/api/share/<token>/"
    # the workspace commit snapshotted into the live bundle
    version: Optional[str] = None
    kind: str = "site"
    file_count: Optional[int] = None
    updated_at: Optional[datetime] = None


class CloudflareConfigIn(BaseModel):
    """Set the owner-level Cloudflare credentials (D-0009 host connector)."""

    api_token: str       # high-privilege deploy token; encrypted at rest, never in a sandbox
    account_id: str


class CloudflareStatusOut(BaseModel):
    """Connector state for the UI — never reveals the token."""

    configured: bool
    account_id: Optional[str] = None


class CloudflareDeployIn(BaseModel):
    """Per-deploy options. Project is per-session; omit to use the session's default."""

    project_name: Optional[str] = None


class CloudflareDeployOut(BaseModel):
    """Result of a Cloudflare Pages deploy."""

    url: str
    project: str


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    title: str
    provider: Optional[str]
    workspace_path: str
    preview_token: str
    status: str
    cf_project: Optional[str] = None  # Cloudflare Pages project this session deploys to
    confidential: bool = False  # P-0009 #1: pinned to a local model
    created_at: datetime
    updated_at: datetime


# ── Provider health (§4.4) ────────────────────────────────────────────────────

class ProviderHealth(BaseModel):
    name: str          # instance id ("claude" or "claude:work")
    template: str      # provider template ("claude") — UI groups instances under this
    label: str         # human display label for the account
    model: Optional[str] = None  # active model for API instances (None for CLI)
    kind: str
    tier: str
    healthy: bool
    cooldown_until: Optional[datetime]
    last_reset_seen: Optional[datetime]
    est_used_pct: Optional[float]
    mode: str  # plan | api | open | mock


class ProviderLimitsUpdate(BaseModel):
    """Operator-declared limit windows (§4.4)."""
    window_seconds: int
    window_limit: int  # estimated cap within the window


class ProviderModelUpdate(BaseModel):
    """Set (or clear, when null/empty) an instance's runtime model override."""
    model: Optional[str] = None


class ConsoleConfig(BaseModel):
    """Whether the scoped in-UI console is available (does not reveal the token)."""
    available: bool


# ── Stats ─────────────────────────────────────────────────────────────────────

class StatsOut(BaseModel):
    runs_today: int
    success_rate: float
    avg_duration_ms: Optional[float]
    runs_by_provider: dict[str, int]
    failover_rate: float
    deferred_now: int
    cost_today_usd: float
    active_runs: int


# ── Credentials (BYO-key) ─────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    provider: str  # template name or instance id ("openai-api" / "openai-api:team")
    api_key: str  # plaintext; encrypted before storage
    label: Optional[str] = None


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    provider: str
    label: Optional[str] = None
    key_hint: Optional[str] = None  # non-secret last-4 ("…wxyz"), never the full key
    created_at: datetime
    last_used_at: Optional[datetime] = None


class UsageSummaryOut(BaseModel):
    """Owner spend surface (P-0009 #2) — today / 7d / by-provider + budget cap."""

    spend_today_usd: float
    spend_7d_usd: float
    by_provider_today: dict[str, float]
    daily_budget_usd: float           # 0 = unlimited
    remaining_today_usd: Optional[float] = None  # None when unlimited
    over_budget: bool


class SecretStatusOut(BaseModel):
    """One row of the named secrets-management surface (P-0009 #3)."""

    provider: str
    tier: str
    env_key: Optional[str] = None
    local: bool = False
    source: str  # "stored" | "env" | "missing"
    key_hint: Optional[str] = None
    last_used_at: Optional[datetime] = None


# ── Mode ─────────────────────────────────────────────────────────────────────

class ModeOut(BaseModel):
    mode: str  # plan | byo_key | hosted
    deployment_mode: str
    plan_cli_allowed: bool
