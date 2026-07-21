"""
tests/test_promotion_projection.py — P-0073: the promotion→projection seam.

Two systems read as one promise from the approver's chair: "approved into
canon" and "projected into sessions". They were not the same. Projection is
driven solely by declared `ContextSource`s and promotion declared none, so
approved canonical writes never reached later sessions — the pilot #43 chain
where an actor briefed to read approved files got a projection that lacked
them and went looking on disk instead.

Two mechanisms, tested here:
  1. **Declare on approval** — approving a canonical write also declares the
     written path, unless the approver opts out or something already covers it.
  2. **Coverage warning** — a projection that is a strict subset of the
     canonical root says so, on the receipt and in the ledger the actor reads.
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


def _make_root(tmp_path, *, manifest: bool = False) -> str:
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# Root\n")
    (root / "docs" / "notes.md").write_text("notes\n")
    if manifest:
        (root / "batonkeep.yaml").write_text(
            "apiVersion: batonkeep.dev/v1alpha1\nkind: Project\nname: p\n"
            "context:\n  bootstrap:\n    - README.md\n"
        )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return str(root)


def _mk_project(c, root) -> str:
    return c.post("/api/projects", json={"name": "P", "root_path": root}).json()["id"]


def _promote(c, pid, rel, content, **decide):
    """propose → approve. Returns the decide response body."""
    ap = c.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": rel, "content": content, "producer": "agent"},
    )
    assert ap.status_code == 202, ap.text
    res = c.post(f"/api/approvals/{ap.json()['id']}/decide",
                 json={"approved": True, **decide})
    assert res.status_code == 200, res.text
    return res.json()


# ── 1. Declare on approval ────────────────────────────────────────────────────

def test_approving_a_promotion_declares_it_as_a_context_source(client, tmp_path):
    """The default: one decision writes canon *and* makes it reachable."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))

    body = _promote(c, pid, "decisions/numbering.md", "# Numbering\ncount up\n")

    declared = body["applied"]["declared_source"]
    assert declared is not None
    assert declared["rel_path"] == "decisions/numbering.md"
    assert declared["kind"] == "file"

    sources = c.get(f"/api/projects/{pid}/context-sources").json()
    assert [s["rel_path"] for s in sources] == ["decisions/numbering.md"]
    # Sensitivity is inherited, never re-guessed at promotion time, and the
    # revision is hashed immediately so freshness is honest from the start.
    assert sources[0]["sensitivity"] == "inherit"
    assert sources[0]["last_revision"]


def test_declaration_appends_after_the_existing_bootstrap_order(client, tmp_path):
    """Promoted canon becomes an ordered read without displacing the manifest's
    own reading priority."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    c.post(f"/api/projects/{pid}/context-sources",
           json={"rel_path": "README.md", "bootstrap_order": 3})

    first = _promote(c, pid, "a.md", "a\n")["applied"]["declared_source"]
    second = _promote(c, pid, "b.md", "b\n")["applied"]["declared_source"]

    assert first["bootstrap_order"] == 4
    assert second["bootstrap_order"] == 5


def test_no_second_declaration_when_a_source_already_covers_the_path(client, tmp_path):
    """Re-declaring a file inside a declared directory would project the same
    bytes twice and split its freshness across two rows."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    c.post(f"/api/projects/{pid}/context-sources",
           json={"rel_path": "docs", "kind": "dir", "bootstrap_order": 0})
    before = c.get(f"/api/projects/{pid}/context-sources").json()[0]["last_revision"]

    body = _promote(c, pid, "docs/new.md", "fresh\n")

    assert body["applied"]["declared_source"] is None
    sources = c.get(f"/api/projects/{pid}/context-sources").json()
    assert [s["rel_path"] for s in sources] == ["docs"]
    # The covering source is re-hashed, so the next projection carries the change.
    assert sources[0]["last_revision"] != before


def test_whole_root_declaration_covers_everything(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    c.post(f"/api/projects/{pid}/context-sources",
           json={"rel_path": ".", "kind": "dir", "bootstrap_order": 0})

    body = _promote(c, pid, "anywhere/deep.md", "x\n")

    assert body["applied"]["declared_source"] is None
    assert len(c.get(f"/api/projects/{pid}/context-sources").json()) == 1


def test_approver_can_decline_the_declaration(client, tmp_path):
    """Opt-out is a real choice — the write still lands, nothing is declared."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    root = c.get(f"/api/projects/{pid}").json()["root_path"]

    body = _promote(c, pid, "scratch.md", "draft\n", declare_source=False)

    assert body["applied"]["declared_source"] is None
    assert c.get(f"/api/projects/{pid}/context-sources").json() == []
    assert os.path.isfile(os.path.join(root, "scratch.md"))  # canon still written


def test_denied_promotion_declares_nothing(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))
    ap = c.post(f"/api/projects/{pid}/context/propose",
                json={"rel_path": "no.md", "content": "x\n", "producer": "agent"})
    res = c.post(f"/api/approvals/{ap.json()['id']}/decide", json={"approved": False})

    assert res.status_code == 200
    assert res.json()["applied"] is None
    assert c.get(f"/api/projects/{pid}/context-sources").json() == []


# ── 2. The seam itself (the pilot #43 regression) ─────────────────────────────

@pytest.mark.asyncio
async def test_approved_canon_reaches_the_next_session(tmp_path, monkeypatch):
    """The whole point of P-0073: promote → the *next* projection carries it.

    Before this, projection was driven solely by declared sources and promotion
    declared none, so an actor briefed to read approved canon received a
    projection that silently lacked it.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    from app import canonical
    from app.db import Base
    from app.models import Approval, Owner, Project
    from app.project_context import project_for_execution

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    root = _make_root(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/seam.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async with Maker() as db:
        db.add(Owner(id="local", label="Me"))
        db.add(Project(id="p1", owner_id="local", name="P", root_path=root))
        await db.commit()

    async with Maker() as db:
        project = await db.get(Project, "p1")
        row = await canonical.propose(
            db, project=project, rel_path="decisions/canon.md",
            content="# Canon\nthe approved decision\n", producer="agent",
        )
        row.status, row.decided_by = "approved", "human"
        await canonical.apply(db, row, project)
        await db.commit()
        assert (await db.get(Approval, row.id)).status == "approved"

    workdir = tmp_path / "ws"
    workdir.mkdir()
    async with Maker() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=None,
            workdir=str(workdir), run_id=None,
        )

    assert [s["rel_path"] for s in receipt.sources] == ["decisions/canon.md"]
    projected = workdir / "context" / "decisions" / "canon.md"
    assert projected.read_text() == "# Canon\nthe approved decision\n"
    await engine.dispose()


# ── 3. Coverage warning ───────────────────────────────────────────────────────

def _project_row(tmp_path, root):
    from app.models import Project

    return Project(id="p1", owner_id="local", name="P", root_path=root)


def test_scan_counts_only_what_no_declared_source_covers(tmp_path):
    from app.models import ContextSource
    from app.project_context import scan_undeclared

    root = _make_root(tmp_path, manifest=True)
    project = _project_row(tmp_path, root)

    # Nothing declared: README.md + docs/notes.md (the manifest is not context).
    assert scan_undeclared(project, []).count == 2

    src = ContextSource(owner_id="local", project_id="p1", kind="dir",
                        rel_path="docs", bootstrap_order=0)
    cov = scan_undeclared(project, [src])
    assert cov.count == 1 and cov.sample == ["README.md"]


def test_scan_is_empty_when_the_root_itself_is_declared(tmp_path):
    from app.models import ContextSource
    from app.project_context import scan_undeclared

    project = _project_row(tmp_path, _make_root(tmp_path))
    src = ContextSource(owner_id="local", project_id="p1", kind="dir", rel_path=".")

    assert scan_undeclared(project, [src]).count == 0


def test_scan_is_bounded_and_reports_the_count_as_a_floor(tmp_path, monkeypatch):
    """The scan is a warning mechanism, so it is bounded rather than exact —
    a huge root must not make every projection walk it."""
    from app.project_context import _settings, scan_undeclared

    root = tmp_path / "big"
    root.mkdir()
    for i in range(30):
        (root / f"f{i}.md").write_text("x\n")
    monkeypatch.setattr(_settings, "context_coverage_scan_max_files", 10)

    cov = scan_undeclared(_project_row(tmp_path, str(root)), [])

    assert cov.truncated is True
    assert 0 < cov.count <= 10


def test_scan_tolerates_a_project_with_no_root(tmp_path):
    from app.models import Project
    from app.project_context import scan_undeclared

    project = Project(id="p1", owner_id="local", name="P", root_path=None)
    assert scan_undeclared(project, []).count == 0


@pytest.mark.asyncio
async def test_projection_records_the_gap_and_tells_the_actor(tmp_path, monkeypatch):
    """The receipt carries the count for the operator; the ledger carries the
    instruction for the actor — say your view is short, do not go hunting."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    from app.db import Base
    from app.models import ContextSource, Owner, Project
    from app.project_context import project_for_execution

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    root = _make_root(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cov.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="Me"))
        db.add(Project(id="p1", owner_id="local", name="P", root_path=root))
        db.add(ContextSource(owner_id="local", project_id="p1", kind="file",
                             rel_path="README.md", bootstrap_order=0))
        await db.commit()

    workdir = tmp_path / "ws"
    workdir.mkdir()
    async with Maker() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=None,
            workdir=str(workdir), run_id=None,
        )

    gap = next(x for x in receipt.exclusions if x["reason"] == "undeclared")
    assert gap["count"] == 1 and gap["sample"] == ["docs/notes.md"]
    assert gap["truncated"] is False

    ledger = (workdir / "WORKITEM.md").read_text()
    assert "## Context coverage" in ledger
    assert "1 file(s)" in ledger
    assert "do not search the filesystem" in ledger
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_fully_declared_root_keeps_its_prior_ledger_bytes(tmp_path, monkeypatch):
    """No warning when there is nothing to warn about — a complete projection's
    ledger is byte-identical to one rendered without the coverage input."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    from app.db import Base
    from app.models import ContextSource, Owner, Project
    from app.project_context import project_for_execution
    from app.work_ledger import render_ledger

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    root = _make_root(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/full.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="Me"))
        db.add(Project(id="p1", owner_id="local", name="P", root_path=root))
        db.add(ContextSource(owner_id="local", project_id="p1", kind="dir",
                             rel_path=".", bootstrap_order=0))
        await db.commit()

    workdir = tmp_path / "ws"
    workdir.mkdir()
    async with Maker() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=None,
            workdir=str(workdir), run_id=None,
        )

    assert receipt.exclusions is None
    ledger = (workdir / "WORKITEM.md").read_text()
    assert "## Context coverage" not in ledger
    assert ledger == render_ledger(project_name="P", work_item=None)
    await engine.dispose()


# ── 4. The operator-facing surface ────────────────────────────────────────────

def test_coverage_endpoint_reports_the_gap_before_a_session_runs(client, tmp_path):
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))

    wide = c.get(f"/api/projects/{pid}/context-coverage").json()
    assert wide["root_bound"] is True
    assert wide["declared_count"] == 0
    assert wide["undeclared_count"] == 2
    assert sorted(wide["sample"]) == ["README.md", "docs/notes.md"]

    # Declaring closes the gap the endpoint reports — the operator's loop.
    c.post(f"/api/projects/{pid}/context-sources",
           json={"rel_path": ".", "kind": "dir", "bootstrap_order": 0})
    closed = c.get(f"/api/projects/{pid}/context-coverage").json()
    assert closed["undeclared_count"] == 0


def test_declaring_one_sampled_path_closes_that_much_of_the_gap(client, tmp_path):
    """The remediation loop the coverage banner drives: declare a path, the
    count drops by exactly that path."""
    c, _ = client
    pid = _mk_project(c, _make_root(tmp_path))

    before = c.get(f"/api/projects/{pid}/context-coverage").json()
    assert before["undeclared_count"] == 2

    r = c.post(f"/api/projects/{pid}/context-sources", json={"rel_path": "docs/notes.md"})
    assert r.status_code == 201, r.text

    after = c.get(f"/api/projects/{pid}/context-coverage").json()
    assert after["undeclared_count"] == 1
    assert after["sample"] == ["README.md"]


@pytest.mark.asyncio
async def test_a_source_declared_without_bootstrap_order_still_projects(tmp_path, monkeypatch):
    """Back-filled declarations carry no bootstrap_order — they must still reach
    the workspace, or the coverage banner's "Declare" button would be theatre."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    from app.db import Base
    from app.models import ContextSource, Owner, Project
    from app.project_context import project_for_execution

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    root = _make_root(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/nb.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="Me"))
        db.add(Project(id="p1", owner_id="local", name="P", root_path=root))
        db.add(ContextSource(owner_id="local", project_id="p1", kind="file",
                             rel_path="README.md", bootstrap_order=0))
        db.add(ContextSource(owner_id="local", project_id="p1", kind="file",
                             rel_path="docs/notes.md", bootstrap_order=None))
        await db.commit()

    workdir = tmp_path / "ws"
    workdir.mkdir()
    async with Maker() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=None,
            workdir=str(workdir), run_id=None,
        )

    # Projected, and ordered after the explicitly-ordered bootstrap reads.
    assert [s["rel_path"] for s in receipt.sources] == ["README.md", "docs/notes.md"]
    assert (workdir / "context" / "docs" / "notes.md").read_text() == "notes\n"
    assert receipt.exclusions is None  # the gap is fully closed
    await engine.dispose()


def test_coverage_endpoint_on_a_project_with_no_root(client):
    c, _ = client
    pid = c.post("/api/projects", json={"name": "Rootless"}).json()["id"]

    body = c.get(f"/api/projects/{pid}/context-coverage").json()

    assert body["root_bound"] is False
    assert body["undeclared_count"] == 0


def test_coverage_endpoint_is_owner_scoped(client):
    c, _ = client
    assert c.get("/api/projects/nope/context-coverage").status_code == 404
