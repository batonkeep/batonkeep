"""
tests/test_canonical_promotion.py — S0.5 by-reference canonical promotion +
context-source raw serving.

Payload v2: promoting evidence into the canonical root pins the evidence
digest at propose time and re-verifies it at apply time — the content never
transits the approval row (so the inline byte cap stops being the promotion
ceiling) and tampering fails closed at either end. The raw route makes the
Context tab's declared sources viewable (the evidence-viewer parity gap).
"""
from __future__ import annotations

import asyncio
import hashlib
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


def _make_root(tmp_path) -> str:
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# Root\n")
    (root / "docs" / "notes.md").write_text("notes\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
         "add", "-A"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "init"], check=True,
    )
    return str(root)


def _mk_project(c, root) -> str:
    return c.post("/api/projects", json={"name": "P", "root_path": root}).json()["id"]


def _capture(client_pair, pid, *, text=None, data=None, filename="m.md"):
    _, Maker = client_pair
    import app.evidence as evidence_store

    async def _cap():
        async with Maker() as db:
            row = await evidence_store.capture(
                db, owner_id="local", project_id=pid, kind="report",
                filename=filename, text=text, data=data,
            )
            await db.commit()
            return row.id, row.digest

    return asyncio.get_event_loop().run_until_complete(_cap())


def test_by_reference_promotion_roundtrip(client, tmp_path):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    eid, digest = _capture(client, pid, text="# Methodology\nfinal\n")

    r = c.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": "docs/methodology.md", "evidence_id": eid},
    )
    assert r.status_code == 202, r.text
    approval = r.json()
    pay = approval["payload"]
    assert pay["v"] == 2 and pay["evidence_id"] == eid and pay["digest"] == digest
    assert "content" not in pay  # by-reference: bytes never transit the row
    assert "+# Methodology" in pay["diff"]
    assert not os.path.exists(os.path.join(root, "docs/methodology.md"))

    d = c.post(f"/api/approvals/{approval['id']}/decide", json={"approved": True})
    assert d.status_code == 200, d.text
    applied = os.path.join(root, "docs/methodology.md")
    assert open(applied).read() == "# Methodology\nfinal\n"
    assert d.json()["applied"]["commit"]  # git root → committed

    # The applied diff is captured as decision evidence.
    kinds = [e["kind"] for e in c.get(f"/api/projects/{pid}/evidence").json()]
    assert "decision" in kinds


def test_tamper_between_propose_and_apply_fails_closed(client, tmp_path):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    eid, _ = _capture(client, pid, text="original\n")

    r = c.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": "docs/x.md", "evidence_id": eid},
    )
    approval = r.json()

    # Alter the stored evidence file after the proposal was pinned.
    ev_file = None
    base = os.path.join(str(tmp_path / "evidence"), f"project_{pid}")
    for name in os.listdir(base):
        ev_file = os.path.join(base, name)
    with open(ev_file, "w") as f:
        f.write("ALTERED\n")

    d = c.post(f"/api/approvals/{approval['id']}/decide", json={"approved": True})
    assert d.status_code == 409
    assert "digest re-verification" in d.json()["detail"]
    assert not os.path.exists(os.path.join(root, "docs/x.md"))
    # Proposal stays pending (approve-or-nothing).
    pending = c.get(f"/api/approvals?status=pending&project_id={pid}").json()
    assert [a["id"] for a in pending] == [approval["id"]]


def test_promotion_guards(client, tmp_path, monkeypatch):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    pid2 = _mk_project(c, _make_root(tmp_path / "other"))
    eid, digest = _capture(client, pid, text="x\n")
    eid_foreign, _ = _capture(client, pid2, text="y\n")

    # Exactly one of content / evidence_id.
    r = c.post(f"/api/projects/{pid}/context/propose",
               json={"rel_path": "a.md", "content": "x", "evidence_id": eid})
    assert r.status_code == 400
    r = c.post(f"/api/projects/{pid}/context/propose", json={"rel_path": "a.md"})
    assert r.status_code == 400

    # Evidence must belong to the target project.
    r = c.post(f"/api/projects/{pid}/context/propose",
               json={"rel_path": "a.md", "evidence_id": eid_foreign})
    assert r.status_code == 400 and "not found in this project" in r.json()["detail"]

    # Caller digest pin must match.
    r = c.post(f"/api/projects/{pid}/context/propose",
               json={"rel_path": "a.md", "evidence_id": eid, "digest": "0" * 64})
    assert r.status_code == 400 and "caller's pin" in r.json()["detail"]

    # Size ceiling names the knob and redirects to the manifest.
    import app.canonical as canonical
    monkeypatch.setattr(canonical._settings, "canonical_max_file_bytes", 1)
    r = c.post(f"/api/projects/{pid}/context/propose",
               json={"rel_path": "a.md", "evidence_id": eid})
    assert r.status_code == 400 and "canonical_max_file_bytes" in r.json()["detail"]


def test_binary_evidence_promotes_bytes_exactly(client, tmp_path):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    blob = b"\x89PNG\r\n\x1a\n" + os.urandom(64)
    eid, digest = _capture(client, pid, data=blob, filename="chart.png")

    r = c.post(
        f"/api/projects/{pid}/context/propose",
        json={"rel_path": "assets/chart.png", "evidence_id": eid},
    )
    assert r.status_code == 202
    assert r.json()["payload"]["diff"].startswith("(binary evidence:")

    d = c.post(f"/api/approvals/{r.json()['id']}/decide", json={"approved": True})
    assert d.status_code == 200
    written = open(os.path.join(root, "assets/chart.png"), "rb").read()
    assert hashlib.sha256(written).hexdigest() == digest


def test_context_source_raw(client, tmp_path):
    c, _ = client
    root = _make_root(tmp_path)
    pid = _mk_project(c, root)
    by_rel = {}
    for rel in ("README.md", "docs"):
        declared = c.post(
            f"/api/projects/{pid}/context-sources", json={"rel_path": rel}
        )
        assert declared.status_code == 201, declared.text
        for s in declared.json()["sources"]:
            by_rel[s["rel_path"]] = s["id"]

    # file source serves directly
    r = c.get(f"/api/projects/{pid}/context-sources/{by_rel['README.md']}/raw")
    assert r.status_code == 200 and r.text == "# Root\n"

    # dir source: 409 bare, 200 with ?path, escape refused
    r = c.get(f"/api/projects/{pid}/context-sources/{by_rel['docs']}/raw")
    assert r.status_code == 409
    r = c.get(
        f"/api/projects/{pid}/context-sources/{by_rel['docs']}/raw",
        params={"path": "notes.md"},
    )
    assert r.status_code == 200 and r.text == "notes\n"
    r = c.get(
        f"/api/projects/{pid}/context-sources/{by_rel['docs']}/raw",
        params={"path": "../README.md"},
    )
    assert r.status_code == 400
