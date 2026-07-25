"""
tests/test_packaging.py — S0.5 workspace package + artifact manifest.

The package is the artifact, not the harness: zip of the tree at git HEAD with
MANIFEST.json (per-file sha256s, commit sha) at zip root, captured as two
append-only evidence rows (`package` + `manifest`). Covers: manifest/zip
correctness + exclusions, dirty/commitless refusal, the size ceiling, and the
API round-trip incl. idempotency per (session × commit).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.sessions import packaging


def _git(ws: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", ws, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _make_workspace(base, name="sess") -> str:
    ws = base / name
    ws.mkdir(parents=True)
    (ws / "index.html").write_text("<h1>site</h1>\n")
    (ws / "data").mkdir()
    (ws / "data" / "series.csv").write_text("q,v\n2026Q1,4.1\n")
    # Harness / projection entries that must NOT enter the package:
    (ws / "SESSION.md").write_text("brief\n")
    (ws / "WORKITEM.md").write_text("ledger\n")
    (ws / "context").mkdir()
    (ws / "context" / "README.md").write_text("projected\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "big.js").write_text("junk\n")
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    _git(str(ws), "add", "-A")
    _git(str(ws), "commit", "-qm", "turn 1")
    return str(ws)


@pytest.mark.asyncio
async def test_build_package_manifest_and_zip(tmp_path):
    ws = _make_workspace(tmp_path)
    manifest, zip_bytes, commit = await packaging.build_package(
        ws, session_id="s" * 32, produced_by="human"
    )

    rels = [f["rel_path"] for f in manifest["files"]]
    assert rels == sorted(rels)
    assert "index.html" in rels and os.path.join("data", "series.csv") in rels
    # Harness/projection/package-manager entries excluded:
    for banned in ("SESSION.md", "WORKITEM.md"):
        assert banned not in rels
    assert not any(r.startswith(("context", "node_modules")) for r in rels)

    assert manifest["v"] == 1
    assert manifest["commit_sha"] == commit
    assert manifest["file_count"] == len(rels)
    idx = next(f for f in manifest["files"] if f["rel_path"] == "index.html")
    assert idx["sha256"] == hashlib.sha256(b"<h1>site</h1>\n").hexdigest()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert packaging.MANIFEST_NAME in names
        assert set(rels) <= set(names)
        embedded = json.loads(zf.read(packaging.MANIFEST_NAME))
        assert embedded == manifest


@pytest.mark.asyncio
async def test_build_package_refuses_commitless_and_dirty(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    with pytest.raises(packaging.PackagingError, match="no committed version"):
        await packaging.build_package(str(bare), session_id="x" * 32, produced_by="human")

    ws = _make_workspace(tmp_path)
    (tmp_path / "sess" / "index.html").write_text("<h1>edited</h1>\n")
    with pytest.raises(packaging.PackagingError, match="uncommitted changes"):
        await packaging.build_package(ws, session_id="x" * 32, produced_by="human")


@pytest.mark.asyncio
async def test_dirty_harness_files_do_not_block_packaging(tmp_path):
    """The ledger rewrites SESSION.md after the turn commit and the projection
    refreshes WORKITEM.md/context/ per execution — a session is 'dirty' on those
    almost always. They're excluded from the package, so they must not 409 it."""
    ws = _make_workspace(tmp_path)
    (tmp_path / "sess" / "SESSION.md").write_text("brief rewritten post-commit\n")
    (tmp_path / "sess" / "WORKITEM.md").write_text("ledger refreshed\n")
    (tmp_path / "sess" / "context" / "README.md").write_text("reprojected\n")
    manifest, _zip, _commit = await packaging.build_package(
        ws, session_id="x" * 32, produced_by="human"
    )
    assert manifest["file_count"] > 0


@pytest.mark.asyncio
async def test_build_package_size_ceiling(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    monkeypatch.setattr(packaging._settings, "package_max_bytes", 4)
    with pytest.raises(packaging.PackageTooLargeError):
        await packaging.build_package(ws, session_id="x" * 32, produced_by="human")


# ── API round-trip ────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    import app.main as main
    import app.sessions.workspace as ws_mod
    from app.db import Base, get_db
    from app.models import Owner, Project, Session, SessionTurn

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    monkeypatch.setattr(ws_mod._settings, "sessions_dir", str(tmp_path / "sessions"))

    sid = "a" * 32
    ws_path = _make_workspace(tmp_path / "sessions", sid)
    head = subprocess.run(
        ["git", "-C", ws_path, "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="Proj"))
            db.add(
                Session(
                    id=sid, owner_id="local", title="t", provider="mock",
                    workspace_path=str(tmp_path / "sessions" / sid),
                    project_id="p1",
                )
            )
            # P-0083 item 5: packages are attributed by commit identity, so the
            # default fixture is the *attributed* case — a turn that produced the
            # commit at HEAD. Tests for the unattributed shapes drop or rewrite it.
            db.add(
                SessionTurn(
                    id=1, session_id=sid, owner_id="local", seq=1, provider="mock",
                    prompt="build it", status="succeeded", commit_sha=head,
                )
            )
            await db.commit()
        return Maker

    Maker = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app), sid, Maker
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_package_route_captures_and_is_idempotent(client, tmp_path):
    c, sid, Maker = client

    r = c.post(f"/api/sessions/{sid}/package")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["existing"] is False
    assert body["package"]["kind"] == "package"
    assert body["manifest"]["kind"] == "manifest"
    assert body["package"]["rel_path"].endswith(".zip")

    # The stored zip's digest matches the row.
    abs_path = os.path.join(
        str(tmp_path / "evidence"), body["package"]["rel_path"]
    )
    with open(abs_path, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == body["package"]["digest"]

    # Second call: same commit → existing rows, nothing new captured.
    r2 = c.post(f"/api/sessions/{sid}/package")
    assert r2.status_code == 200
    assert r2.json()["existing"] is True
    assert r2.json()["package"]["id"] == body["package"]["id"]

    ev = c.get("/api/projects/p1/evidence")
    assert ev.status_code == 200
    kinds = [e["kind"] for e in ev.json()]
    assert kinds.count("package") == 1 and kinds.count("manifest") == 1


def test_package_route_refuses_dirty(client, tmp_path):
    c, sid, Maker = client
    (tmp_path / "sessions" / sid / "index.html").write_text("<h1>dirty</h1>\n")
    r = c.post(f"/api/sessions/{sid}/package")
    assert r.status_code == 409
    assert "uncommitted" in r.json()["detail"]


# ── P-0083 item 5: the package cannot claim a delivery that did not happen ─────
#
# R5 (`DRILL-I049-R5-on-I043`): a fully escaped Agy turn committed nothing, yet
# `Capture package` snapshotted the session's *initial* commit and stamped it with
# that turn + its WorkItem — evidence #140/#141, `file_count=0`, project evidence
# count 116 → 118, for work that never entered the workspace.

def _clear_turns(Maker):
    """Drop the fixture's producing turn: HEAD becomes an unattributable baseline
    commit, exactly like a session whose only turn escaped."""
    from sqlalchemy import delete

    from app.models import SessionTurn

    async def _go():
        async with Maker() as db:
            await db.execute(delete(SessionTurn))
            await db.commit()
    asyncio.get_event_loop().run_until_complete(_go())


def test_package_route_refuses_unattributed_baseline(client):
    c, sid, Maker = client
    _clear_turns(Maker)

    r = c.post(f"/api/sessions/{sid}/package")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "no session turn produced commit" in detail
    assert "allow_unattributed" in detail          # the operator is told the way out

    # Nothing was captured — the evidence ledger must not advance for a refusal.
    ev = c.get("/api/projects/p1/evidence")
    assert [e["kind"] for e in ev.json()] == []


def test_package_route_refuses_when_producing_turn_escaped(client):
    """The partial-escape shape: the turn *did* commit (so it is attributable by
    commit identity) but reported outputs the workspace never received. Its commit
    must not be packageable as that turn's delivery."""
    c, sid, Maker = client

    async def _mark_escaped():
        from app.models import SessionTurn
        async with Maker() as db:
            turn = await db.get(SessionTurn, 1)
            turn.output_flags = {
                "v": 1,
                "unbacked": ["file:///home/agent/.gemini/scratch/canary.txt"],
                "escaped_workspace": True,
                "escape_scope": "partial",
            }
            await db.commit()
    asyncio.get_event_loop().run_until_complete(_mark_escaped())

    r = c.post(f"/api/sessions/{sid}/package")
    assert r.status_code == 409, r.text
    assert "escaped_workspace" in r.json()["detail"]


def test_package_route_baseline_capture_carries_no_attribution(client, tmp_path):
    """The explicit non-delivery path: allowed, but it may not claim a turn, a work
    item, or a pin — the manifest says `baseline` so a later reader cannot mistake
    it for delivered work."""
    c, sid, Maker = client
    _clear_turns(Maker)

    r = c.post(f"/api/sessions/{sid}/package", json={"allow_unattributed": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["package"]["session_turn_id"] is None
    assert body["package"]["work_item_id"] is None
    assert body["manifest"]["session_turn_id"] is None

    abs_path = os.path.join(str(tmp_path / "evidence"), body["package"]["rel_path"])
    with zipfile.ZipFile(abs_path) as zf:
        embedded = json.loads(zf.read(packaging.MANIFEST_NAME))
    assert embedded["attribution"] == "baseline"


def test_package_route_baseline_cannot_be_pinned_to_a_work_item(client):
    c, sid, Maker = client
    _clear_turns(Maker)

    r = c.post(
        f"/api/sessions/{sid}/package",
        json={"allow_unattributed": True, "pin_to_work_item_id": 1},
    )
    assert r.status_code == 409, r.text
    assert "unattributed baseline" in r.json()["detail"]


def test_attributed_package_records_the_producing_turn(client):
    """The healthy path stays intact and is attributed to the turn that produced
    the packaged commit — by commit identity, not recency."""
    c, sid, Maker = client

    async def _add_later_escaped_turn():
        from app.models import SessionTurn
        async with Maker() as db:
            db.add(
                SessionTurn(
                    id=2, session_id=sid, owner_id="local", seq=2, provider="mock",
                    prompt="escape", status="succeeded", commit_sha=None,
                    output_flags={"v": 1, "unbacked": ["x"], "escaped_workspace": True},
                )
            )
            await db.commit()
    asyncio.get_event_loop().run_until_complete(_add_later_escaped_turn())

    r = c.post(f"/api/sessions/{sid}/package")
    assert r.status_code == 200, r.text
    # Turn 2 is the latest turn, but turn 1 produced the commit being packaged.
    assert r.json()["package"]["session_turn_id"] == 1
