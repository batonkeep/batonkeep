"""
models.py — SQLAlchemy ORM models.

Multi-tenancy note (§8): every owned row carries owner_id (default "local").
In personal/oss mode this is invisible in the UI but enables a clean migration to
managed (H3) without a schema rewrite.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Owner(Base):
    """Seeded with one "local" owner in personal/oss; becomes a user table in managed."""

    __tablename__ = "owners"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), default="Local operator")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    tasks: Mapped[list[Task]] = relationship(back_populates="owner")
    runs: Mapped[list[Run]] = relationship(back_populates="owner")
    credentials: Mapped[list[Credential]] = relationship(back_populates="owner")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # {key: value} filled into prompt via str.format_map with defaultdict
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # "none" | "interval" | "cron"
    schedule_kind: Mapped[str] = mapped_column(String(16), default="none")
    # cron expression or interval seconds
    schedule_expr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # IANA timezone the cron expression is interpreted in (e.g. "America/Los_Angeles").
    # "UTC" by default; ignored for interval/none schedules.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    want_markdown: Mapped[bool] = mapped_column(Boolean, default=True)
    want_json: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Routing policy JSON (§4.3)
    routing: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Code-exec execution policy (P-0046): off | confirmation | allow-safe | auto.
    # Unattended tasks have no human to confirm, so a task must explicitly carry
    # allow-safe/auto to use code-exec; the conservative default is confirmation.
    exec_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmation")
    # Image-generation model override (P-0046 slice 6 follow-up), same semantics as
    # Session.image_model_id: NULL = inherit the text provider's catalog default;
    # otherwise a catalog id (possibly cross-provider) from image_models.py.
    image_model_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # Per-task generated-asset retention (P-0050/D-0046). NULL = unlimited (default).
    # When set, after each run the oldest stored run-assets are pruned until the
    # task's total stored asset count / byte size is back under the cap.
    asset_max_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asset_max_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[Owner] = relationship(back_populates="tasks")
    runs: Mapped[list[Run]] = relationship(back_populates="task", cascade="all, delete-orphan")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=False, index=True
    )
    # "manual" | "schedule"
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    # "queued" | "running" | "succeeded" | "failed" | "deferred" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # provider instance id that ultimately produced the result (e.g. "claude:work")
    provider: Mapped[str | None] = mapped_column(String(96), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ordered list of attempt outcomes: [{provider, outcome, reset_at?}]
    attempts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Bounded in-process auto-retries consumed on transient failures (P-0025 #2).
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Optional dedupe key: a second enqueue with the same key + an active run is a
    # no-op (returns the existing run) rather than a duplicate (P-0025 #2).
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    overflow_used: Mapped[bool] = mapped_column(Boolean, default=False)
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    subagents: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    markdown_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    json_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[Owner] = relationship(back_populates="runs")
    task: Mapped[Task] = relationship(back_populates="runs")
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.seq"
    )
    assets: Mapped[list[RunAsset]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunAsset.id"
    )

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None


class RunAsset(Base):
    """A non-md/json artifact a task run produced (generated image, agent-written
    CSV/PDF, etc.), captured from the agent's `current/` scratch into the run's
    canonical outputs dir (P-0050/D-0046). The text deliverables stay on
    `Run.markdown_path` / `Run.json_path`; everything else is a row here.

    `rel_path` is relative to the run's outputs dir (e.g. "assets/generated-1.png");
    the serving route joins it under /data/outputs/run_<id>. Promoted read-only into
    the task's history alongside output.md/json so a later run can read but not
    mutate it.
    """

    __tablename__ = "run_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("runs.id"), nullable=False, index=True
    )
    rel_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped[Run] = relationship(back_populates="assets")


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # EventKind: log | phase | token | tool | subagent | result | error | route
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    run: Mapped[Run] = relationship(back_populates="events")


class Session(Base):
    """
    A build session (M1.1): an interactive, multi-turn conversation with a chosen
    CLI-plan agent against a sandboxed, git-init'd per-session workspace.

    The workspace filesystem — not the chat transcript — is the source of truth
    (D-0008). `provider` holds the currently-selected agent instance id; switching
    it routes the *next* turn to a different executor, which continues from the
    workspace + SESSION.md brief rather than a replayed transcript.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid hex
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="Untitled session")
    # currently-selected provider instance id (e.g. "grok", "agy", "mock")
    provider: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # Per-session model override for the chosen API provider (P-0049). NULL = use the
    # provider's catalog `preferred.default`. Applies to API-path providers only (CLI
    # plans own their model via their own config dir). Threaded into the executor as
    # extra['model'] for the turn.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # absolute path to the sandboxed workspace directory
    workspace_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # unguessable token gating the live preview (M1.2). Required on every preview
    # request so a session's workspace is never reachable without session auth.
    preview_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # "active" | "archived"
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # Cloudflare Pages project this session deploys to (D-0009). The token+account
    # are owner-level (encrypted store); the *project* is per-session so each build
    # gets its own site. Remembered after the first deploy; defaults from the title.
    cf_project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Sovereignty toggle (P-0009 #1): when set, every turn is pinned to a local
    # model — the workspace + prompt never leave the box, and a remote provider
    # selection is overridden to a local one (fail closed if none is available).
    confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Code-exec execution policy (P-0046): off | confirmation | allow-safe | auto.
    # Default confirmation — code-exec is offered only under allow-safe/auto until
    # the interactive approval round-trip lands (slice 3b).
    exec_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmation")
    # Image-generation model override (P-0046 slice 6 follow-up). NULL = inherit the
    # text provider's catalog default; otherwise a catalog id from
    # `app/providers/image_models.py`, which may be cross-provider (e.g. a Grok image
    # model on an OpenAI-text session). The chosen model's home provider must have a
    # credential for image gen to be offered.
    image_model_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # Optional per-session spend cap (USD, API path). NULL = no session cap (opt-in).
    # Enforced cumulatively across turns at round boundaries by the executor budget
    # gate; composes with the owner daily cap (whichever is lower wins). Stop-at-
    # next-step semantics — bounded overshoot ≤ one round (you can't halt mid-call).
    budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    turns: Mapped[list[SessionTurn]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="SessionTurn.seq"
    )


class SessionTurn(Base):
    """One user→agent exchange within a Session. Records which provider handled it."""

    __tablename__ = "session_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # the provider instance id that handled this turn (records the agent switch)
    provider: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # the effective model id this turn ran (P-0049) — feeds per-model usage metrics
    # for the catalog picker's most/recently-used sort, alongside Run.model for tasks.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "running" | "succeeded" | "failed"
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # M1.3 versioning: the workspace commit this turn produced (None if the turn
    # changed no files), plus a `git --stat` summary for the History/event view.
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    diffstat: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D-0017 thread 2: per-file artifact list (JSON) the turn produced — the result
    # surfaced to the user is the workspace files changed, not scraped agent text.
    changed_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Token/cost usage for this turn (API path). Build-session spend was previously
    # not metered — these let it surface in Analytics + count toward the budget.
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Prompt-cache token split (API path). cache_read = replayed input billed at the
    # cache-read rate; cache_write = first store of a cache breakpoint (a premium).
    # Both 0 when caching is off; cost_usd already reflects their rates.
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[Session] = relationship(back_populates="turns")


class Artifact(Base):
    """
    A published build artifact (M1.4): a static snapshot of a session's workspace,
    served publicly at a revocable share URL.

    Publish materializes the workspace's static assets (excluding .git and the
    internal SESSION.md brief) into a bundle dir and mints a `share_token`; the
    public route `/api/share/{token}` serves that bundle. Revoke clears the token
    (→ 404) and removes the bundle. One artifact per session: re-publishing updates
    this row and rotates the token. `version` records the workspace commit that was
    snapshotted, so the live site is stable until the next publish.
    """

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False, unique=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="site")
    # the workspace commit snapshotted at publish time
    version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # public, unguessable share token; NULL when revoked/never published
    share_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # absolute path to the materialized publish bundle dir (NULL when revoked)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Credential(Base):
    """BYO-key encrypted at rest with APP_SECRET (§4.1 / §8)."""

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    # credential key id — the template name ("openai-api") for the default account,
    # or an instance id ("openai-api:team") for an extra same-provider subscription.
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    # optional human label for the account (e.g. "Work key")
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    # Non-secret last-4 fingerprint so the secrets surface can distinguish keys
    # without ever decrypting them. Set at write time; never the full value.
    key_hint: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Touched whenever an executor resolves this key — turns the store into an
    # observable surface ("is this key actually in use, and when last?").
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[Owner] = relationship(back_populates="credentials")
