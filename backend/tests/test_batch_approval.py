"""
tests/test_batch_approval.py — P-0077: batch canonical approval.

Canonical promotion was one approval per file. The pilot backlog — ~23 entries
plus 10 heading remediations plus 3 reports — was parked rather than promoted
because 35+ individual gestures cost more than the promotions were worth.

A batch is **one decision and one audit event** but deliberately **not one
transaction**: per-row atomicity is what makes it usable for draining a queue,
since one unappliable proposal must not block the other thirty-four. The two
things a batch can get wrong that a single decision cannot — approving a set
whose members overwrite each other, and approving content written against a
root that has since moved — are refused and flagged respectively.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            await db.commit()
        return Maker

    Maker = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app), Maker
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def _make_root(tmp_path, name="root") -> str:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Root\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return str(root)


def _mk_project(c, root, name="P") -> str:
    return c.post("/api/projects", json={"name": name, "root_path": root}).json()["id"]


def _propose(c, pid, rel, content="x\n") -> int:
    r = c.post(f"/api/projects/{pid}/context/propose",
               json={"rel_path": rel, "content": content, "producer": "agent"})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _capture(client_pair, pid, *, text):
    """Capture an evidence row; returns (id, rel_path)."""
    _, Maker = client_pair
    import app.evidence as evidence_store

    async def _cap():
        async with Maker() as db:
            row = await evidence_store.capture(
                db, owner_id="local", project_id=pid, kind="report",
                filename="m.md", text=text,
            )
            await db.commit()
            return row.id, row.rel_path

    return asyncio.get_event_loop().run_until_complete(_cap())


def _batch(c, ids, approved=True, **kw):
    return c.post("/api/approvals/batch-decide",
                  json={"approval_ids": ids, "approved": approved, **kw})


# ── The point: clearing a queue ───────────────────────────────────────────────

def test_a_set_is_promoted_in_one_decision(client, tmp_path):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    ids = [_propose(c, pid, f"entries/e{i}.md", f"# Entry {i}\n") for i in range(5)]

    res = _batch(c, ids)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["decided"] == 5 and body["failed"] == 0
    assert {r["outcome"] for r in body["results"]} == {"decided"}
    for i in range(5):
        assert open(os.path.join(root, f"entries/e{i}.md")).read() == f"# Entry {i}\n"
    assert c.get("/api/approvals?status=pending").json() == []


def test_every_row_carries_the_shared_batch_id(client, tmp_path):
    """The set is a property of the decision, so it must be reconstructable
    afterwards — not inferrable from a timestamp window."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    ids = [_propose(c, pid, f"a{i}.md") for i in range(3)]

    batch_id = _batch(c, ids).json()["batch_id"]

    rows = c.get("/api/approvals?status=approved").json()
    assert {r["batch_id"] for r in rows} == {batch_id}
    assert len(batch_id) <= 32


def test_individually_decided_rows_carry_no_batch_id(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    aid = _propose(c, pid, "solo.md")

    c.post(f"/api/approvals/{aid}/decide", json={"approved": True})

    assert c.get("/api/approvals").json()[0]["batch_id"] is None


def test_a_batch_can_deny_a_set(client, tmp_path):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    ids = [_propose(c, pid, f"no{i}.md") for i in range(3)]

    body = _batch(c, ids, approved=False).json()

    assert body["decided"] == 3 and body["approved"] is False
    assert [r["status"] for r in c.get("/api/approvals").json()] == ["denied"] * 3
    assert not os.path.exists(os.path.join(root, "no0.md"))


def test_declare_source_carries_through_the_batch(client, tmp_path):
    """P-0073's declaration is per-decision, so a batch decision declares the
    whole set — otherwise clearing a queue would silently skip the fix."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    ids = [_propose(c, pid, f"canon/c{i}.md") for i in range(3)]

    _batch(c, ids)

    declared = {s["rel_path"] for s in c.get(f"/api/projects/{pid}/context-sources").json()}
    assert declared == {"canon/c0.md", "canon/c1.md", "canon/c2.md"}


def test_declaration_can_be_declined_for_the_whole_batch(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    ids = [_propose(c, pid, f"d{i}.md") for i in range(2)]

    _batch(c, ids, declare_source=False)

    assert c.get(f"/api/projects/{pid}/context-sources").json() == []


# ── One decision, but not one transaction ─────────────────────────────────────

def test_one_unappliable_proposal_does_not_block_the_rest(client, tmp_path):
    """The property that makes this usable on a real backlog. A by-reference
    proposal whose evidence has vanished cannot apply — and must not take the
    other proposals down with it."""
    c, Maker = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)

    good = [_propose(c, pid, "ok1.md"), _propose(c, pid, "ok2.md")]
    eid, rel = _capture(client, pid, text="# from evidence\n")
    bad = c.post(f"/api/projects/{pid}/context/propose",
                 json={"rel_path": "broken.md", "evidence_id": eid,
                       "producer": "agent"}).json()["id"]
    # The evidence file disappears between propose and decide.
    from app.evidence import evidence_abs_path
    os.unlink(evidence_abs_path(pid, rel))

    body = _batch(c, [*good, bad]).json()

    assert body["decided"] == 2 and body["failed"] == 1
    failed = next(r for r in body["results"] if r["outcome"] == "failed")
    assert failed["approval_id"] == bad
    assert failed["error"]
    assert os.path.isfile(os.path.join(root, "ok1.md"))
    assert not os.path.exists(os.path.join(root, "broken.md"))
    # The failed row stays decidable — it was never settled.
    pending = c.get("/api/approvals?status=pending").json()
    assert [r["id"] for r in pending] == [bad]


# ── What a batch can get wrong that a single decision cannot ──────────────────

def test_same_path_collisions_are_refused(client, tmp_path):
    """Proposals carry whole file bodies. Applying two for one path means the
    second silently discards the first, while the approver — having selected
    both — would reasonably believe both landed."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    first = _propose(c, pid, "shared.md", "# version A\n")
    second = _propose(c, pid, "shared.md", "# version B\n")
    other = _propose(c, pid, "unrelated.md")

    res = _batch(c, [first, second, other])

    assert res.status_code == 409
    assert "shared.md" in res.json()["detail"]
    # Nothing was decided — the refusal is a precondition, not a partial run.
    assert len(c.get("/api/approvals?status=pending").json()) == 3


def test_stale_proposals_are_flagged_on_listing(client, tmp_path):
    """A proposal written against a root that has since moved will discard what
    landed underneath it. Advisory, not blocking — but visible before deciding."""
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    stale = _propose(c, pid, "README.md", "# rewritten\n")
    fresh = _propose(c, pid, "new.md", "# new\n")

    # The root moves underneath the pending proposal.
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write("# changed by someone else\n")

    rows = {r["id"]: r for r in c.get("/api/approvals?status=pending").json()}
    assert rows[stale]["stale"] is True
    assert rows[fresh]["stale"] is False


def test_decided_rows_are_not_marked_stale(client, tmp_path):
    """Staleness is only meaningful for something still actionable."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    aid = _propose(c, pid, "README.md", "# rewritten\n")
    c.post(f"/api/approvals/{aid}/decide", json={"approved": True})

    assert c.get("/api/approvals").json()[0]["stale"] is None


# ── Preconditions ─────────────────────────────────────────────────────────────

def test_batch_is_capped(client, tmp_path, monkeypatch):
    import app.main as main

    c, _ = client
    monkeypatch.setattr(main.settings, "approval_batch_max", 2)
    pid = _mk_project(c, _make_root(tmp_path))
    ids = [_propose(c, pid, f"x{i}.md") for i in range(3)]

    res = _batch(c, ids)

    assert res.status_code == 400 and "capped at 2" in res.json()["detail"]


def test_empty_batch_is_refused(client):
    c, _ = client
    assert _batch(c, []).status_code == 400


def test_a_batch_decides_one_project(client, tmp_path):
    """The audit event is project-scoped, so the set must be too."""
    c, _ = client
    p1 = _mk_project(c, _make_root(tmp_path, "r1"), "One")
    p2 = _mk_project(c, _make_root(tmp_path, "r2"), "Two")

    res = _batch(c, [_propose(c, p1, "a.md"), _propose(c, p2, "b.md")])

    assert res.status_code == 400 and "one project" in res.json()["detail"]


def test_already_decided_rows_are_refused(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    done = _propose(c, pid, "done.md")
    live = _propose(c, pid, "live.md")
    c.post(f"/api/approvals/{done}/decide", json={"approved": True})

    res = _batch(c, [done, live])

    assert res.status_code == 409 and "already decided" in res.json()["detail"]


def test_unknown_ids_are_refused(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    res = _batch(c, [_propose(c, pid, "a.md"), 9999])

    assert res.status_code == 404 and "9999" in res.json()["detail"]


def test_code_exec_approvals_are_not_decidable_here(client, tmp_path):
    """Same fence as the single-decision route — a blocked turn's Future must
    not be bypassable through the batch path."""
    c, Maker = client
    pid = _mk_project(c, _make_root(tmp_path))
    ok = _propose(c, pid, "a.md")

    async def _add():
        from app.models import Approval
        async with Maker() as db:
            db.add(Approval(owner_id="local", request_id="ce-1", kind="code_exec",
                            status="pending", project_id=pid,
                            payload={"v": 1, "code": "print(1)"}, producer="agent"))
            await db.commit()
            return (await db.get(Approval, 2)).id

    ce = asyncio.get_event_loop().run_until_complete(_add())
    res = _batch(c, [ok, ce])

    assert res.status_code == 400 and "canonical_write" in res.json()["detail"]


# ── The audit event ───────────────────────────────────────────────────────────

def test_the_batch_writes_one_audit_event(client, tmp_path):
    """Per-file decision evidence still exists for forensics; this row is what
    says these were decided together, by one person, at one moment."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    ids = [_propose(c, pid, f"e{i}.md") for i in range(3)]

    batch_id = _batch(c, ids).json()["batch_id"]

    ev = c.get(f"/api/projects/{pid}/evidence").json()
    summary = [e for e in ev if e["rel_path"].endswith(f"batch_{batch_id[:12]}.md")]
    assert len(summary) == 1
    body = c.get(f"/api/evidence/{summary[0]['id']}/raw").text
    assert batch_id in body
    assert "3 approved, 0 failed" in body
    for i in range(3):
        assert f"e{i}.md" in body
    # …alongside, not instead of, the per-file decision rows.
    assert len([e for e in ev if e["kind"] == "decision"]) == 4
