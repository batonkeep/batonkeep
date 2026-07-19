"""
tests/test_evidence_projection.py — S0.5 evidence loop: project-wide index,
pinned-evidence materialization (digest fail-closed), receipt.evidence payload,
and pin validation on the work-item PATCH.

The dogfood finding this closes: the ledger's evidence index used to filter to
the *bound* work item, so a fresh work item's cold session saw nothing of its
predecessors' outputs. The index is now project-wide, and a work item's pinned
evidence is materialized read-only under `context/evidence/` so a cold operator
holds the actual inputs, not just their names.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from app.project_context import project_for_execution


@pytest.fixture
async def proj_db(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/proj.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="T"))
        await db.commit()
    yield Maker
    await engine.dispose()


async def _seed(Maker, tmp_path, monkeypatch):
    """Project + two work items; WI-A owns two captured evidence files, plus one
    project-scoped row. Returns (wi_a, wi_b, [evidence ids])."""
    import app.evidence as evidence_store
    from app.models import Project, WorkItem

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").write_text("truth\n")

    async with Maker() as db:
        db.add(Project(id="p1", owner_id="local", name="P1", root_path=str(root)))
        wi_a = WorkItem(owner_id="local", project_id="p1", title="A", state="done",
                        objective="build")
        wi_b = WorkItem(owner_id="local", project_id="p1", title="B", state="open",
                        objective="review")
        db.add_all([wi_a, wi_b])
        await db.flush()
        e1 = await evidence_store.capture(
            db, owner_id="local", project_id="p1", kind="report",
            filename="report.md", text="# findings\n", work_item_id=wi_a.id,
        )
        e2 = await evidence_store.capture(
            db, owner_id="local", project_id="p1", kind="diff",
            filename="turn.diff", text="+++ site\n", work_item_id=wi_a.id,
        )
        e3 = await evidence_store.capture(
            db, owner_id="local", project_id="p1", kind="log",
            filename="run.log", text="ok\n",  # no work item — project-scoped
        )
        await db.commit()
        return wi_a.id, wi_b.id, [e1.id, e2.id, e3.id]


async def test_index_is_project_wide_and_receipted(proj_db, tmp_path, monkeypatch):
    wi_a, wi_b, ids = await _seed(proj_db, tmp_path, monkeypatch)
    workdir = tmp_path / "work"
    workdir.mkdir()

    async with proj_db() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=wi_b,
            workdir=str(workdir), session_turn_id=None,
        )

    ledger = (workdir / "WORKITEM.md").read_text()
    # A fresh work item's ledger lists its predecessors' evidence + origin tags.
    assert f"[WI-{wi_a}] report:" in ledger
    assert f"[WI-{wi_a}] diff:" in ledger
    assert "[project] log:" in ledger
    assert f"· evidence {ids[0]}" in ledger

    ev = receipt.evidence
    assert ev["v"] == 1 and ev["index_count"] == 3
    assert ev["materialized"] == [] and ev["exclusions"] == []
    assert len(ev["index_sha"]) == 64


async def test_pinned_evidence_materializes_and_tamper_fails_closed(
    proj_db, tmp_path, monkeypatch
):
    import app.evidence as evidence_store
    from app.models import Evidence, WorkItem

    wi_a, wi_b, ids = await _seed(proj_db, tmp_path, monkeypatch)

    # Tamper with e2's stored file after capture; pin a nonexistent id too.
    async with proj_db() as db:
        e2 = await db.get(Evidence, ids[1])
        tampered = evidence_store.evidence_abs_path(e2.project_id, e2.rel_path)
        with open(tampered, "w") as f:
            f.write("ALTERED\n")
        wi = await db.get(WorkItem, wi_b)
        wi.pinned_evidence = {"v": 1, "items": [
            {"evidence_id": ids[0]}, {"evidence_id": ids[1]}, {"evidence_id": 9999},
        ]}
        await db.commit()

    workdir = tmp_path / "work"
    workdir.mkdir()
    async with proj_db() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=wi_b,
            workdir=str(workdir), session_turn_id=None,
        )

    ev = receipt.evidence
    # e1 materialized read-only with its digest intact:
    assert len(ev["materialized"]) == 1
    m = ev["materialized"][0]
    assert m["evidence_id"] == ids[0]
    dest = workdir / m["rel_path"]
    assert dest.is_file()
    assert not os.access(dest, os.W_OK)
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == m["digest"]
    # The ledger points the cold operator at its inputs:
    ledger = (workdir / "WORKITEM.md").read_text()
    assert "## Inputs (pinned evidence)" in ledger and m["rel_path"] in ledger
    # Tampered + missing pins fail closed as recorded exclusions:
    reasons = {x["evidence_id"]: x["reason"] for x in ev["exclusions"]}
    assert reasons[ids[1]] == "digest-mismatch"
    assert reasons[9999] == "missing"
    # And the tampered file was never materialized:
    ev_dir = workdir / "context" / "evidence"
    assert not any(p.name.startswith(f"{ids[1]}_") for p in ev_dir.iterdir())


async def test_pinned_evidence_budget_excludes(proj_db, tmp_path, monkeypatch):
    import app.project_context as pc
    from app.models import WorkItem

    wi_a, wi_b, ids = await _seed(proj_db, tmp_path, monkeypatch)
    monkeypatch.setattr(pc._settings, "context_evidence_max_bytes", 1)

    async with proj_db() as db:
        wi = await db.get(WorkItem, wi_b)
        wi.pinned_evidence = {"v": 1, "items": [{"evidence_id": ids[0]}]}
        await db.commit()

    workdir = tmp_path / "work"
    workdir.mkdir()
    async with proj_db() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=wi_b,
            workdir=str(workdir), session_turn_id=None,
        )
    assert receipt.evidence["materialized"] == []
    assert receipt.evidence["exclusions"] == [
        {"evidence_id": ids[0], "reason": "budget"}
    ]


# ── Pin validation on the work-item PATCH ────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner, Project, WorkItem

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
            db.add(Project(id="p1", owner_id="local", name="P"))
            db.add(Project(id="p2", owner_id="local", name="Other"))
            db.add(WorkItem(owner_id="local", project_id="p1", title="W",
                            objective="o"))
            e = await evidence_store.capture(
                db, owner_id="local", project_id="p1", kind="report",
                filename="r.md", text="x\n",
            )
            e_foreign = await evidence_store.capture(
                db, owner_id="local", project_id="p2", kind="report",
                filename="f.md", text="y\n",
            )
            await db.commit()
            return Maker, e.id, e_foreign.id

    Maker, eid, eid_foreign = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app), eid, eid_foreign
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_patch_pins_validate(client, monkeypatch):
    c, eid, eid_foreign = client

    r = c.patch("/api/work-items/1", json={"pinned_evidence": [eid, eid]})
    assert r.status_code == 200, r.text
    assert r.json()["pinned_evidence"] == {"v": 1, "items": [{"evidence_id": eid}]}

    # Evidence from another project is refused.
    r = c.patch("/api/work-items/1", json={"pinned_evidence": [eid_foreign]})
    assert r.status_code == 400
    assert "not found in this project" in r.json()["detail"]

    # The pin cap is enforced.
    import app.main as main
    monkeypatch.setattr(main.settings, "evidence_pin_max", 1)
    r = c.patch("/api/work-items/1", json={"pinned_evidence": [eid, eid + 1000]})
    assert r.status_code == 400 and "at most 1" in r.json()["detail"]

    # Empty list clears the pins.
    r = c.patch("/api/work-items/1", json={"pinned_evidence": []})
    assert r.status_code == 200
    assert r.json()["pinned_evidence"] is None
