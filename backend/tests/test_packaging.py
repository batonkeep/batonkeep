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
    from app.models import Owner, Project, Session

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    monkeypatch.setattr(ws_mod._settings, "sessions_dir", str(tmp_path / "sessions"))

    sid = "a" * 32
    _make_workspace(tmp_path / "sessions", sid)

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
            await db.commit()
        return Maker

    Maker = asyncio.get_event_loop().run_until_complete(_setup())

    async def _get_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(main.app), sid
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_package_route_captures_and_is_idempotent(client, tmp_path):
    c, sid = client

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
    c, sid = client
    (tmp_path / "sessions" / sid / "index.html").write_text("<h1>dirty</h1>\n")
    r = c.post(f"/api/sessions/{sid}/package")
    assert r.status_code == 409
    assert "uncommitted" in r.json()["detail"]
