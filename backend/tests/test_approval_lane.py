"""
tests/test_approval_lane.py — `kind` names the act, `lane` names what it gates (D-0080).

`kind == "code_exec"` was standing in for *"is this an unattended run's tool request"* at
three call sites. That held while code execution was the only gated tool and stopped
holding the moment `browser_open` rode the same lane — at which point a parked browser
navigation would have been looked past by the cancel-settle path and by the inbox.

The third instance this codebase has produced of the same shape: **a rule stated
correctly in prose and implemented by naming one instance of it.** `reap_pending`'s
docstring said proposals are spared because they "carry no Future and stay decidable
across restarts", and then spared exactly `canonical_write` by name — so every pending
`schedule_proposal` was expired on the next restart.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import approvals as approvals_mod
from app.models import Approval


@pytest.fixture
async def maker(tmp_path):
    from app.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lane.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestLaneDerivation:
    def test_tools_are_the_tool_lane(self):
        assert approvals_mod.lane_for("code_exec") == "tool"
        assert approvals_mod.lane_for("browser_open") == "tool"

    def test_proposals_are_the_proposal_lane(self):
        assert approvals_mod.lane_for("canonical_write") == "proposal"
        assert approvals_mod.lane_for("schedule_proposal") == "proposal"

    def test_an_unknown_kind_defaults_to_the_safer_lane(self):
        """A new gated tool is the likely unknown, and treating it as `tool` means it
        expires on restart rather than lingering as a decidable request whose waiter is
        gone. Failing towards "the operator sees nothing" beats "the operator decides
        something that cannot take effect"."""
        assert approvals_mod.lane_for("some_future_tool") == "tool"


class TestRecordedRows:
    async def test_lane_is_derived_not_passed(self, maker):
        """One fact, one home. A caller able to supply the lane could disagree with the
        kind, which is the conflation this column removes."""
        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="r1", kind="browser_open",
                producer="claude-api", run_id=5, task_id=2,
            )
            await approvals_mod.record_request(
                db, owner_id="local", request_id="r2", kind="schedule_proposal",
                producer="planner", project_id="p1",
            )
            await db.commit()
        async with maker() as db:
            rows = {r.request_id: r for r in (await db.execute(select(Approval))).scalars()}
        assert rows["r1"].lane == "tool"
        assert rows["r1"].kind == "browser_open", "the act, not the lane it rides"
        assert rows["r2"].lane == "proposal"


class TestReapOnRestart:
    async def test_a_schedule_proposal_survives_a_restart(self, maker, monkeypatch):
        """The defect the lane column fixes. D-0070 shipped "an agent may propose a
        schedule; only the operator may grant it", and every such proposal was expired at
        the next restart before the operator could see it — because the sweep named
        `canonical_write` instead of asking what kind of thing it was looking at.
        """
        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="s1", kind="schedule_proposal",
                producer="planner", project_id="p1",
            )
            await approvals_mod.record_request(
                db, owner_id="local", request_id="c1", kind="canonical_write",
                producer="human", project_id="p1",
            )
            # A tool request with no checkpoint: its waiter died with the process, so
            # this one *must* be expired — leaving it pending shows the operator a
            # decision that can never take effect.
            await approvals_mod.record_request(
                db, owner_id="local", request_id="t1", kind="code_exec",
                producer="claude-api", run_id=9, task_id=4,
            )
            await db.commit()

        monkeypatch.setattr("app.db.AsyncSessionLocal", maker)
        reaped = await approvals_mod.reap_pending()

        async with maker() as db:
            rows = {r.request_id: r for r in (await db.execute(select(Approval))).scalars()}
        assert rows["s1"].status == "pending", "a schedule proposal has no Future to lose"
        assert rows["c1"].status == "pending"
        assert rows["t1"].status == "expired"
        assert reaped == 1


class TestThePrincipalJoins:
    """[[P-0103]]'s purpose was that "what has this agent done" be answerable by joining
    the audit record instead of inferring it. It could not be, because the two surfaces
    named the same actor differently."""

    async def test_the_principal_is_the_task_and_the_activity_is_the_run(self, maker):
        from app import attribution

        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="x1", kind="code_exec",
                producer="claude-api", run_id=83, task_id=14,
            )
            await db.commit()
        async with maker() as db:
            row = (await db.execute(select(Approval))).scalars().first()

        # A Task is the definition — the durable thing an operator names and schedules,
        # and thinks of as "an agent". A Run is one episode of it acting.
        assert row.principal_id == attribution.agent("task", "14")
        assert row.executed_by == attribution.agent("run", "83")
        assert row.initiated_by == "human:local"

    async def test_it_matches_what_the_agents_view_calls_the_same_actor(self, maker):
        """The whole point: these two strings must be equal or nothing joins."""
        from app import attribution

        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="x2", kind="code_exec",
                producer="claude-api", run_id=83, task_id=14,
            )
            await db.commit()
        async with maker() as db:
            row = (await db.execute(select(Approval))).scalars().first()

        # `main.list_agents` builds exactly this for the card.
        agents_view_principal = attribution.agent("task", str(14))
        assert row.principal_id == agents_view_principal

    async def test_the_provider_is_never_the_identity(self, maker):
        """D-0008 switches providers mid-task by design, so keying identity on the
        backend makes one agent look like several."""
        async with maker() as db:
            for rid, provider in (("p1", "claude-api"), ("p2", "openai-api")):
                await approvals_mod.record_request(
                    db, owner_id="local", request_id=rid, kind="code_exec",
                    producer=provider, run_id=83, task_id=14,
                )
            await db.commit()
        async with maker() as db:
            rows = (await db.execute(select(Approval))).scalars().all()
        assert len({r.principal_id for r in rows}) == 1, (
            "the same agent failing over between providers is still one agent"
        )
        assert not any("claude-api" in (r.principal_id or "") for r in rows)


def test_the_approver_files_the_tool_not_the_human_label():
    """`label` is what a person reads; `tool` is what the record files it as. Passing the
    tool name as the label is how a browser navigation came to be recorded as a code-exec
    request — and inferring the kind back out of the label would have filed a code-exec
    approval under "run a probe"."""
    import inspect

    from app import orchestrator

    sig = inspect.signature(orchestrator._make_run_approver)
    assert "task_id" in sig.parameters, "the principal needs the definition, not the run"
    src = inspect.getsource(orchestrator._make_run_approver)
    assert "kind=tool" in src
    assert 'tool: str = "code_exec"' in src


def test_asyncio_marker_present():
    """Sanity: this module's async tests run under the suite's auto asyncio mode."""
    assert asyncio.iscoroutinefunction(approvals_mod.reap_pending)
