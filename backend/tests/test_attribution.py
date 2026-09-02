"""P-0103 / D-0059 D3 — the attribution envelope.

D-0059 marks these fields **can't-retrofit**: rows accumulate, and an approval written
without an envelope is an adjudication whose actor can never be recovered. So the tests
here are less about the shape than about two properties — that the envelope is *always*
populated at the write sites, and that it distinguishes the three actors D3 names, so
`delegated_by` already has somewhere to go when agents can ask each other for work.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app import attribution


class TestScheme:
    def test_principals_are_typed_and_namespaced(self):
        """`producer` is free-form and conflates human/system/provider — which is why it
        cannot be queried, and why this exists alongside it."""
        assert attribution.human("local") == "human:local"
        assert attribution.system("scheduler") == "system:scheduler"
        assert attribution.agent("planner", "project-abc") == "agent:planner/project-abc"

    def test_an_agent_is_not_identified_by_its_provider(self):
        """Which model answered can change mid-task by design (D-0008 cross-provider
        switching). Keying identity on the provider would record the backend rather than
        the actor, and make one agent look like several."""
        a = attribution.by_agent("run", "task-12", initiated_by=attribution.human())
        assert a.principal_id == "agent:run/task-12"
        assert "claude" not in a.principal_id and "openai" not in a.principal_id

    def test_agent_work_still_traces_back_to_a_human(self):
        """Today every agent action traces to a human who scheduled or started it, so
        `initiated_by` is the operator and `delegated_by` is honestly NULL."""
        a = attribution.by_agent("planner", "p1")
        assert a.initiated_by == "human:local"
        assert a.executed_by == "agent:planner/p1"
        assert a.delegated_by is None

    def test_delegation_is_expressible_before_it_is_used(self):
        """The agent-to-agent case, which is the whole reason this lands now: when A asks
        B, the record must say so, and the field must already exist to say it in."""
        a = attribution.by_agent(
            "run", "task-9",
            initiated_by="agent:planner/p1", delegated_by="agent:planner/p1",
        )
        assert a.delegated_by == "agent:planner/p1"
        assert a.executed_by == "agent:run/task-9"
        assert a.principal_kind == attribution.KIND_AGENT

    def test_system_is_distinct_from_agent(self):
        """A scheduler firing decided nothing. Calling it an agent overstates what
        happened; calling it the operator is simply false."""
        s = attribution.by_system("scheduler")
        assert s.principal_kind == attribution.KIND_SYSTEM
        assert s.principal_kind != attribution.KIND_AGENT


@pytest.fixture
def maker(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner, Project

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/attr.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="P"))
            await db.commit()
        return Maker

    loop = asyncio.get_event_loop()
    Maker = loop.run_until_complete(_setup())
    try:
        yield Maker
    finally:
        loop.run_until_complete(engine.dispose())


def test_every_approval_is_written_with_an_envelope(maker):
    """The can't-retrofit property: no call site can forget it, because it is derived at
    the write rather than passed in."""
    from app import approvals as approvals_mod
    from app.models import Approval

    async def _body():
        async with maker() as db:
            await approvals_mod.record_request(
                db, owner_id="local", request_id="a1", kind="code_exec",
                producer="claude-api", run_id=None,
            )
            await approvals_mod.record_request(
                db, owner_id="local", request_id="a2", kind="canonical_write",
                producer="human", project_id="p1",
            )
            await db.commit()
        async with maker() as db:
            rows = {r.request_id: r for r in (await db.execute(select(Approval))).scalars()}
        return rows

    rows = asyncio.get_event_loop().run_until_complete(_body())

    agent_row = rows["a1"]
    assert agent_row.principal_kind == "agent"
    assert agent_row.executed_by == "agent:run/claude-api"
    assert agent_row.initiated_by == "human:local", "agent work traces back to a human"

    human_row = rows["a2"]
    assert human_row.principal_kind == "human"
    assert human_row.principal_id == "human:local"


def test_evidence_carries_the_envelope_too(maker, tmp_path, monkeypatch):
    """Evidence is the content-bearing half of the audit record, so "who produced this"
    is what makes the row citable later."""
    import app.evidence as ev
    from app.models import Evidence

    monkeypatch.setattr(ev._settings, "evidence_dir", str(tmp_path / "ev"))

    async def _body():
        async with maker() as db:
            await ev.capture(
                db, owner_id="local", project_id="p1", kind="report",
                filename="r.md", text="hello", producer="grok",
            )
            await db.commit()
        async with maker() as db:
            return (await db.execute(select(Evidence))).scalars().one()

    row = asyncio.get_event_loop().run_until_complete(_body())
    assert row.principal_kind == "agent"
    assert row.executed_by == "agent:run/grok"
    assert row.delegated_by is None, "NULL until agents delegate — an honest absence"
