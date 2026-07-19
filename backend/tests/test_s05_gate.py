"""
tests/test_s05_gate.py — the S0.5 closing proof (cold handoff v2), cross-slice.

The full loop the slices built, exercised end-to-end through the real API +
projection: a session's workspace at git HEAD is captured as a package
(zip + MANIFEST.json) and **pinned to the next work item in the same call**;
a fresh execution for that work item then materializes the package read-only
into its own workspace, where the embedded manifest's per-file digests match
the producing tree exactly — a cold operator holds a verifiable copy of its
predecessor's artifact from durable Project state alone.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    import app.main as main
    import app.sessions.workspace as ws_mod
    from app.db import Base, get_db
    from app.models import Owner, Project, Session, WorkItem

    # raising=False: earlier suite members "restore" shared Settings fields by
    # popping them from the instance __dict__, which on pydantic v2 deletes the
    # field — a plain setattr would then error on the missing attribute.
    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence"),
        raising=False,
    )
    monkeypatch.setattr(
        ws_mod._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False
    )

    # Producing session workspace: committed artifact files + harness entries.
    sid = "a" * 32
    ws = tmp_path / "sessions" / sid
    ws.mkdir(parents=True)
    (ws / "index.html").write_text("<h1>dashboard</h1>\n")
    (ws / "data").mkdir()
    (ws / "data" / "cpi.csv").write_text("q,v\n2026Q1,4.1\n")
    (ws / "SESSION.md").write_text("brief\n")
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t",
         "add", "-A"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "build"], check=True,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="P"))
            wi_a = WorkItem(owner_id="local", project_id="p1", title="build",
                            objective="ship it", state="done")
            wi_b = WorkItem(owner_id="local", project_id="p1", title="review",
                            objective="reproduce + critique", state="open")
            db.add_all([wi_a, wi_b])
            await db.flush()
            db.add(Session(id=sid, owner_id="local", title="t", provider="mock",
                           workspace_path=str(ws), project_id="p1",
                           work_item_id=wi_a.id))
            await db.commit()
            return Maker, wi_a.id, wi_b.id

    Maker, wi_a, wi_b = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app), Maker, sid, wi_a, wi_b, ws
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_cold_handoff_v2_package_to_next_work_item(env, tmp_path):
    c, Maker, sid, wi_a, wi_b, ws = env

    # 1. Capture the producing workspace AND hand it to the review work item.
    r = c.post(f"/api/sessions/{sid}/package", json={"pin_to_work_item_id": wi_b})
    assert r.status_code == 200, r.text
    pkg = r.json()["package"]

    # The pin landed on WI-B (visible over the normal work-item API).
    items = c.get("/api/projects/p1/work-items").json()
    target = next(w for w in items if w["id"] == wi_b)
    assert target["pinned_evidence"] == {"v": 1, "items": [{"evidence_id": pkg["id"]}]}

    # Idempotent re-capture with the same pin adds nothing twice.
    r2 = c.post(f"/api/sessions/{sid}/package", json={"pin_to_work_item_id": wi_b})
    assert r2.status_code == 200 and r2.json()["existing"] is True
    target = next(w for w in c.get("/api/projects/p1/work-items").json()
                  if w["id"] == wi_b)
    assert len(target["pinned_evidence"]["items"]) == 1

    # 2. A fresh execution for WI-B materializes the package.
    from app.project_context import project_for_execution

    cold = tmp_path / "cold"
    cold.mkdir()

    async def _project():
        async with Maker() as db:
            return await project_for_execution(
                db, owner_id="local", project_id="p1", work_item_id=wi_b,
                workdir=str(cold), session_turn_id=None,
            )

    receipt = asyncio.get_event_loop().run_until_complete(_project())
    mat = receipt.evidence["materialized"]
    assert [m["evidence_id"] for m in mat] == [pkg["id"]]

    # 3. The materialized zip's embedded manifest digests match the producing
    #    tree byte-for-byte — the artifact is reproducible, not just named.
    zip_path = cold / mat[0]["rel_path"]
    with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
        for entry in manifest["files"]:
            src = ws / entry["rel_path"]
            assert hashlib.sha256(src.read_bytes()).hexdigest() == entry["sha256"]
            assert hashlib.sha256(zf.read(entry["rel_path"])).hexdigest() == entry["sha256"]
    rels = [f["rel_path"] for f in manifest["files"]]
    assert "index.html" in rels and "SESSION.md" not in rels

    # 4. The cold ledger points the operator at the input.
    ledger = (cold / "WORKITEM.md").read_text()
    assert "## Inputs (pinned evidence)" in ledger
    assert mat[0]["rel_path"] in ledger

    # 5. Pin validation still fences cross-project targets.
    r = c.post(f"/api/sessions/{sid}/package", json={"pin_to_work_item_id": 9999})
    assert r.status_code == 400
