"""
models.py — SQLAlchemy ORM models.

Multi-tenancy note (§8): every owned row carries owner_id (default "local").
In personal/oss mode this is invisible in the UI but enables a clean migration to
managed (H3) without a schema rewrite.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

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

    tasks: Mapped[list["Task"]] = relationship(back_populates="owner")
    runs: Mapped[list["Run"]] = relationship(back_populates="owner")
    credentials: Mapped[list["Credential"]] = relationship(back_populates="owner")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # {key: value} filled into prompt via str.format_map with defaultdict
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # "none" | "interval" | "cron"
    schedule_kind: Mapped[str] = mapped_column(String(16), default="none")
    # cron expression or interval seconds
    schedule_expr: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # IANA timezone the cron expression is interpreted in (e.g. "America/Los_Angeles").
    # "UTC" by default; ignored for interval/none schedules.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    want_markdown: Mapped[bool] = mapped_column(Boolean, default=True)
    want_json: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Routing policy JSON (§4.3)
    routing: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["Owner"] = relationship(back_populates="tasks")
    runs: Mapped[list["Run"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owners.id"), nullable=False, default="local", index=True
    )
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    # "manual" | "schedule"
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    # "queued" | "running" | "succeeded" | "failed" | "deferred" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # provider instance id that ultimately produced the result (e.g. "claude:work")
    provider: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tier: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # ordered list of attempt outcomes: [{provider, outcome, reset_at?}]
    attempts: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    overflow_used: Mapped[bool] = mapped_column(Boolean, default=False)
    deferred_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    subagents: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    markdown_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    json_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["Owner"] = relationship(back_populates="runs")
    task: Mapped["Task"] = relationship(back_populates="runs")
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.seq"
    )

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # EventKind: log | phase | token | tool | subagent | result | error | route
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="events")


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
    provider: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    # absolute path to the sandboxed workspace directory
    workspace_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # unguessable token gating the live preview (M1.2). Required on every preview
    # request so a session's workspace is never reachable without session auth.
    preview_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # "active" | "archived"
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    turns: Mapped[list["SessionTurn"]] = relationship(
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
    provider: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "running" | "succeeded" | "failed"
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped["Session"] = relationship(back_populates="turns")


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
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owner: Mapped["Owner"] = relationship(back_populates="credentials")
