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


class Project(Base):
    """The durable top-level unit of work (S0 substrate): an ongoing project owns
    context declarations, work items, evidence, and the task/session/run history
    produced under it — and outlives any single session or provider.

    Every owner gets exactly one `is_default` "Personal workspace" Project
    (created by migration/backfill); tasks and sessions created without an
    explicit project resolve to it, so existing clients keep working unchanged.
    `root_path` optionally binds the project to an external context root (a
    checkout or directory) whose manifest declares canonical sources — the DB
    never becomes a competing truth store for those files.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid hex
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="Untitled project")
    # Free-form label (general | infra | research | …). Labels only — engine code
    # must never branch on it (the substrate stays domain-neutral).
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    # "active" | "archived"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # "normal" | "confidential" — composes with the existing per-session/task
    # confidential pin; project-level enforcement arrives with policy work.
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    # Exactly one per owner; undeletable; the resolution target for legacy clients.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Absolute path of the bound context root (NULL = no external context yet).
    root_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Manifest path relative to root_path (parser lands in the next slice).
    manifest_rel: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    work_items: Mapped[list[WorkItem]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class WorkItem(Base):
    """A durable unit of intent under a Project — the thing a run or session is
    *for*. It may span multiple sessions, providers, and days; it owns the
    objective, state, small structured decisions, and the honest next action
    (the fields a cold provider handoff is reconstructed from). Large content
    belongs in Evidence, not here.
    """

    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False, index=True
    )
    # Free taxonomy: task | incident | change | investigation | review | chore | …
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="task")
    # open | in_progress | awaiting_approval | blocked | done | dropped | reopened
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="open", index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    # Durable intent — what "done" means, independent of any transcript.
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The single honest next step; agents propose it, the orchestrator writes it.
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    # low | medium | high — recorded, not yet enforced.
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True
    )
    # Content-safe origin reference: {source, kind, external_id, ts}.
    signal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Append-only list of small structured decisions: [{ts, actor, text}].
    decisions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Operator-curated *inputs* for this work item — evidence a projection
    # materializes into the workspace so a cold operator has its predecessors'
    # outputs in hand: {"v": 1, "items": [{"evidence_id", "note"?}]}. Pins live
    # here (work items are mutable state) so the Evidence table itself stays
    # append-only with no update path.
    pinned_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # P-0069 item 6 (B2): the sub-task checklist = output contract + grounded
    # progress. {"v":1,"items":[{id,label,expected?,status,done,verified,...}]}.
    # A verifiable item (declares `expected`) is done+verified only when its glob
    # matches the committed tree; asserted items stay unverified. Agent-proposed,
    # operator-confirmed (see app/subtasks.py + [[P-0078]] planner).
    subtasks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="work_items")


class ContextSource(Base):
    """A declared canonical input of a Project (from its manifest): a file, dir,
    or git checkout relative to the project root. The row records *where truth
    lives* and its last-seen revision — never the content itself. Freshness is
    surfaced to humans; nothing auto-rewrites a stale source.
    """

    __tablename__ = "context_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False, index=True
    )
    # git | dir | file
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="file")
    # Relative to Project.root_path — keeps exports portable across machines.
    rel_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # Ordering for bootstrap reads; NULL = not a bootstrap source.
    bootstrap_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Grouping label from the manifest (e.g. a domain directory).
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # inherit | normal | confidential
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="inherit")
    # git HEAD sha (kind=git) or sha256 content hash (file/dir).
    last_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ContextReceipt(Base):
    """What an actor actually received for one run/turn — source revisions,
    projection version, exclusions, and the working-ledger hash. Content-safe by
    construction (paths + hashes only, never file content), so a receipt can
    always be retained, exported, and audited regardless of sensitivity.

    Receipts point at the run/turn they served (not the other way around), so
    the execution tables carry no receipt FK and a receipt survives its run's
    crash — it is persisted before the executor starts.
    """

    __tablename__ = "context_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False, index=True
    )
    work_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("runs.id"), nullable=True, index=True
    )
    session_turn_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("session_turns.id"), nullable=True, index=True
    )
    # Partitions receipts when the projection algorithm changes (cf.
    # RoutingDecision.policy_version).
    projection_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="proj-v1"
    )
    # [{source_id, rel_path, revision}] — never content.
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # sha256 of the projected working-ledger text (deterministic given equal inputs).
    ledger_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # [{rel_path, reason}] — sensitivity/budget cuts, surfaced not silent.
    exclusions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # What evidence the actor received (still paths + hashes, never content):
    # {"v": 1, "index_count", "index_sha", "materialized": [{evidence_id,
    # rel_path, digest}], "exclusions": [{evidence_id, reason}]}.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approx_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Provenance stamps: without these, provider-fit comparisons can't
    # distinguish a model regression from a CLI/harness regression. The harness
    # version is stamped at projection time; the CLI version is filled in when
    # a CLI-lane candidate actually starts (last candidate attempted wins on
    # failover — it is the one whose output the run records).
    harness_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cli_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Evidence(Base):
    """An append-only, attributable record of what happened: a report, diff,
    log, or verification captured to the evidence store and indexed here with a
    content digest. Rows are created and read — there is deliberately no update
    path; retention deletion is the only removal.
    """

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False, index=True
    )
    work_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("runs.id"), nullable=True, index=True
    )
    session_turn_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("session_turns.id"), nullable=True, index=True
    )
    # report | diff | log | verification | decision | asset-ref
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="report")
    # Relative to the project's evidence dir (serving joins it, run-assets style).
    rel_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # sha256 of the file content at capture time.
    digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Actor: "human", a provider instance id, or "system".
    producer: Mapped[str] = mapped_column(String(96), nullable=False, default="system")
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="inherit")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Approval(Base):
    """A durable human-approval record. The in-process Future (approvals.py)
    remains the wakeup mechanism for a coroutine awaiting a decision; this row
    is the record — it survives a restart (stale pending rows are expired by
    the startup reaper, mirroring run/turn reaping) and is the audit trail the
    substrate's approval baseline builds on.

    kinds: code_exec (session confirmation round-trip) · canonical_write
    (proposed change to a project's canonical context; payload carries the
    diff, applied by the engine only on approval).
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    # Bridges the in-process Future registry (approvals.request) and the WS event.
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="code_exec")
    # pending | approved | denied | expired
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending",
                                        index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True, index=True
    )
    work_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("runs.id"), nullable=True, index=True
    )
    # Versioned payload ({"v": 1, ...}): code_exec carries {code, label};
    # canonical_write carries {rel_path, content, diff, base_revision}.
    # Additive-only evolution — readers tolerate unknown keys, never migrate.
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Who proposed / who decided ("human", provider instance id, or "system").
    producer: Mapped[str] = mapped_column(String(96), nullable=False, default="system")
    decided_by: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    # S0 substrate: the Project this task belongs to. Nullable at the DB level
    # during the staged migration; the API resolves a missing value to the
    # owner's default Project and always writes it.
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True, index=True
    )
    work_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True
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
    # Per-task run timeout in seconds (P-0056/D-0052). NULL = inherit the global
    # run_timeout_seconds default (1800s). Bounds elapsed wall-clock time for the
    # whole run; clamped to a 6h ceiling at the API boundary.
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # S0 substrate: inherited from the task at enqueue time so run history stays
    # queryable by project even if the task is later moved.
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True, index=True
    )
    work_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True, index=True
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
    # P-0069: output advisories mirroring session_turns.output_flags — the
    # `outputs_missing` sub-task-contract check on the task lane. NULL = clean.
    output_flags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
    routing_decisions: Mapped[list[RoutingDecision]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RoutingDecision.id"
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


class RoutingDecision(Base):
    """A structured record of one routing decision (P-0053) — what the router
    considered and *why*, captured at decision time by `router.RoutingTrace`.

    This is the spine of the smart-routing backbone: it accumulates the
    (context → route → outcome) tuples a later scored/learned policy needs, which
    cannot be retrofitted from history we never captured. The router stays DB-free;
    the orchestrator persists this from the trace it returns.

    Content-free by construction — only candidate metadata + decision features, never
    prompt/output (so it respects the D-0017/D-0022 telemetry posture; confidential
    runs may record the *decision* — local, audience-A — but never content). Linked to
    the run it routed; `run_id` is nullable so session-level routing can reuse the table
    later. A scored-policy seam is the follow-on slice.

    **Outcome linkage (P-0053 slice 2)** — the `outcome_*` / `executed_*` / `failover_used`
    / `attempt_count` fields are filled at run finalization, closing the
    (decision → realized outcome) tuple a scored policy learns from: did the chosen
    route succeed, on the first try or only after failover, and at what cost/latency.
    These are *outcome-derived* routing signals — honest and immediately available. A
    richer **user-acceptance** quality signal (kept-vs-regenerated, explicit feedback)
    is deliberately a later slice tied to session-level routing + UI, not inferred here.
    """

    __tablename__ = "routing_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("runs.id"), nullable=True, index=True
    )
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # S0 substrate: project + work-item *type* only (content-free, like task_id —
    # plain columns, no FK, so a deleted project never blocks telemetry rows).
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    work_item_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Which policy made the call (router.POLICY_VERSION) — partitions decisions when
    # a scored/learned policy later replaces or augments the rule-based one.
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="rule-v1")
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    # Constraint layers in force at decision time.
    confidential: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deployment_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deciding_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The originally-declared candidate set (before sovereignty/budget rewrite).
    requested_candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Per-candidate features + status at decision time (see RoutingTrace.evaluated).
    evaluated: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Primary chosen instance (chosen_candidates[0]) + the full ordered failover list.
    chosen: Mapped[str | None] = mapped_column(String(96), nullable=True)
    chosen_candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    overflow_to: Mapped[str | None] = mapped_column(String(96), nullable=True)

    # ── Outcome linkage (P-0053 slice 2) — filled at run finalization ──────────
    # Terminal run status: succeeded | failed | deferred | cancelled.
    outcome_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The instance/model that actually produced the result (may differ from `chosen`).
    executed_provider: Mapped[str | None] = mapped_column(String(96), nullable=True)
    executed_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # True iff the primary choice didn't produce the result (fell back / overflowed) —
    # the cleanest route-quality signal: "did my top pick work first try".
    failover_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Realized cost/latency of the chosen route — core optimization targets for a
    # future scored policy, denormalized so the tuple is self-contained.
    outcome_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run | None] = relationship(back_populates="routing_decisions")


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
    # S0 substrate: the Project this session belongs to (default-resolved like Task).
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True, index=True
    )
    work_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_items.id"), nullable=True
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
    # "running" | "succeeded" | "failed" | "cancelled" (P-0057/D-0051: user interrupt)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # M1.3 versioning: the workspace commit this turn produced (None if the turn
    # changed no files), plus a `git --stat` summary for the History/event view.
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    diffstat: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D-0017 thread 2: per-file artifact list (JSON) the turn produced — the result
    # surfaced to the user is the workspace files changed, not scraped agent text.
    changed_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P-0069 item 6 (free default check): file paths the response links to that are
    # NOT backed by this session's committed tree — {"v":1,"unbacked":[...]}. NULL =
    # nothing flagged. Advisory (a turn can still succeed); the P43-D3 misdirected-
    # writes tell, made machine-checkable instead of only human-visible.
    output_flags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
