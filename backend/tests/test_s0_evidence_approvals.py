"""
tests/test_s0_evidence_approvals.py — S0 slice 3: evidence store + approval baseline.

Covers (spec Verify S0.3):
  • evidence capture — file + row + digest, text through the secrets wall,
    traversal-guarded serving, append-only (no update route);
  • canonical writes — propose never touches the root; approve applies +
    git-commits on git roots + re-hashes the source + captures decision
    evidence; deny changes nothing;
  • durable approval rows — pending survives as a row, startup reaper expires
    orphaned code_exec rows but keeps canonical-write proposals decidable;
  • provenance stamps — receipts carry harness_version; the CLI probe caches.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.redact import REDACTED


# ── Client fixture (metadata-built DB, the substrate-test pattern) ────────────

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
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def _git_root(tmp_path) -> str:
    """A minimal git-backed context root with a manifest-declared source file."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "NOTES.md").write_text("original truth\n")
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


# ── Evidence store ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_writes_redacted_file_with_digest(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.evidence as evidence_store
    from app.db import Base
    from app.models import Owner, Project

    monkeypatch.setattr(
        evidence_store._settings, "evidence_dir", str(tmp_path / "evidence")
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ev.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            project = Project(id="p1", owner_id="local", name="P")
            db.add(project)
            await db.commit()

            row = await evidence_store.capture(
                db, owner_id="local", project_id="p1", kind="report",
                filename="out.md",
                text="report body\nOPENAI_API_KEY=sk-proj-Abc123XyzAbc123XyzAbc\n",
                producer="mock",
            )
            await db.commit()

        # The written file is redacted, and the digest matches the bytes on disk.
        path = evidence_store.evidence_abs_path("p1", row.rel_path)
        assert path and os.path.isfile(path)
        data = open(path, "rb").read()
        assert b"sk-proj" not in data
        assert REDACTED.encode() in data
        assert hashlib.sha256(data).hexdigest() == row.digest
        assert row.bytes == len(data)
        assert row.rel_path.startswith("project_p1/")
    finally:
        await engine.dispose()


def test_evidence_traversal_guard(monkeypatch, tmp_path):
    import app.evidence as evidence_store

    monkeypatch.setattr(evidence_store._settings, "evidence_dir", str(tmp_path))
    assert evidence_store.evidence_abs_path("p1", "../outside") is None
    assert evidence_store.evidence_abs_path("p1", "project_p2/steal") is None
    assert evidence_store.evidence_abs_path("p1", "project_p1/ok.md") is not None


def test_evidence_has_no_update_route(client):
    # Append-only by construction: the collection has list + raw only. FastAPI
    # answers 404 (no such path) or 405 (path exists, method doesn't) — either
    # proves the mutation surface is absent.
    assert client.patch("/api/evidence/1", json={}).status_code in (404, 405)
    assert client.put("/api/evidence/1", json={}).status_code in (404, 405)
    assert client.delete("/api/evidence/1").status_code in (404, 405)


# ── Canonical writes (approval baseline) ──────────────────────────────────────

def test_propose_never_touches_root_and_approve_applies(client, tmp_path):
    root = _git_root(tmp_path)
    project = client.post(
        "/api/projects", json={"name": "Infra", "root_path": root}
    ).json()

    resp = client.post(
        f"/api/projects/{project['id']}/context/propose",
        json={"rel_path": "NOTES.md", "content": "updated truth\n", "producer": "mock"},
    )
    assert resp.status_code == 202
    approval = resp.json()
    assert approval["status"] == "pending"
    assert approval["kind"] == "canonical_write"
    assert "-original truth" in approval["payload"]["diff"]
    assert "+updated truth" in approval["payload"]["diff"]

    # S0.3: without approval the root is untouched.
    assert (tmp_path / "root" / "NOTES.md").read_text() == "original truth\n"

    decision = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"approved": True}
    )
    assert decision.status_code == 200
    body = decision.json()
    assert body["approval"]["status"] == "approved"
    assert body["applied"]["rel_path"] == "NOTES.md"
    # Applied to the root, committed on the git root.
    assert (tmp_path / "root" / "NOTES.md").read_text() == "updated truth\n"
    head_msg = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "canonical write: NOTES.md" in head_msg
    assert body["applied"]["commit"]

    # Decision evidence was captured (append-only record of the applied diff).
    evidence = client.get(f"/api/projects/{project['id']}/evidence").json()
    assert any(e["kind"] == "decision" for e in evidence)

    # Deciding twice is refused.
    again = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"approved": True}
    )
    assert again.status_code == 409


def test_deny_changes_nothing(client, tmp_path):
    root = _git_root(tmp_path)
    project = client.post(
        "/api/projects", json={"name": "Infra", "root_path": root}
    ).json()
    approval = client.post(
        f"/api/projects/{project['id']}/context/propose",
        json={"rel_path": "NOTES.md", "content": "rogue edit\n"},
    ).json()

    decision = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"approved": False}
    )
    assert decision.status_code == 200
    assert decision.json()["approval"]["status"] == "denied"
    assert decision.json()["applied"] is None
    assert (tmp_path / "root" / "NOTES.md").read_text() == "original truth\n"


def test_propose_rejects_escaping_paths(client, tmp_path):
    root = _git_root(tmp_path)
    project = client.post(
        "/api/projects", json={"name": "Infra", "root_path": root}
    ).json()
    resp = client.post(
        f"/api/projects/{project['id']}/context/propose",
        json={"rel_path": "../outside.md", "content": "x"},
    )
    assert resp.status_code == 400


def test_proposal_content_passes_secrets_wall(client, tmp_path):
    root = _git_root(tmp_path)
    project = client.post(
        "/api/projects", json={"name": "Infra", "root_path": root}
    ).json()
    approval = client.post(
        f"/api/projects/{project['id']}/context/propose",
        json={"rel_path": "NOTES.md",
              "content": "key: sk-proj-Abc123XyzAbc123XyzAbc\n"},
    ).json()
    assert "sk-proj" not in approval["payload"]["content"]
    assert REDACTED in approval["payload"]["content"]


# ── Durable approval rows ─────────────────────────────────────────────────────

def test_reaper_expires_code_exec_but_keeps_canonical(client, tmp_path, monkeypatch):
    import app.approvals as approvals
    from app.models import Approval

    root = _git_root(tmp_path)
    project = client.post(
        "/api/projects", json={"name": "Infra", "root_path": root}
    ).json()
    canonical = client.post(
        f"/api/projects/{project['id']}/context/propose",
        json={"rel_path": "NOTES.md", "content": "later\n"},
    ).json()

    # A stranded code_exec row (its Future died with the "previous process").
    # reap_pending opens app.db.AsyncSessionLocal — point it at the test DB by
    # building a maker over the same engine the client's override uses.
    import app.main as main
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import get_db
    get_db_override = main.app.dependency_overrides[get_db]

    async def _reap_with_patched_sessionlocal():
        agen = get_db_override()
        db = await anext(agen)
        maker = async_sessionmaker(db.bind, expire_on_commit=False)
        monkeypatch.setattr("app.db.AsyncSessionLocal", maker)
        db.add(Approval(owner_id="local", request_id="deadbeef", kind="code_exec",
                        status="pending", producer="mock"))
        await db.commit()
        await agen.aclose()
        return await approvals.reap_pending()

    reaped = asyncio.get_event_loop().run_until_complete(
        _reap_with_patched_sessionlocal()
    )
    assert reaped == 1

    rows = client.get("/api/approvals").json()
    by_rid = {r["request_id"]: r for r in rows}
    assert by_rid["deadbeef"]["status"] == "expired"
    # The canonical-write proposal is NOT reaped — still decidable post-restart.
    assert by_rid[canonical["request_id"]]["status"] == "pending"


def test_approvals_owner_scoped(client):
    rows = client.get("/api/approvals?status=pending").json()
    assert rows == []


# ── Provenance stamps (A2) ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_receipt_carries_harness_version(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import project_context
    from app.db import Base
    from app.models import Owner, Project
    from app.version import APP_VERSION

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/rc.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            db.add(Project(id="p1", owner_id="local", name="P"))
            await db.commit()
            workdir = tmp_path / "wd"
            workdir.mkdir()
            receipt = await project_context.project_for_execution(
                db, owner_id="local", project_id="p1", workdir=str(workdir),
            )
        assert receipt is not None
        assert receipt.harness_version == APP_VERSION[:32]
        assert receipt.cli_version is None  # stamped only when a CLI candidate starts
    finally:
        await engine.dispose()


def test_cli_version_probe_caches(monkeypatch):
    from app.providers import cli_executor

    calls = {"n": 0}

    class _Out:
        stdout = "fakecli 9.9.9\nextra line"
        stderr = ""

    def _fake_run(*args, **kwargs):
        calls["n"] += 1
        return _Out()

    monkeypatch.setattr("subprocess.run", _fake_run)
    cli_executor._CLI_VERSION_CACHE.clear()
    assert cli_executor.probe_cli_version("fakecli") == "fakecli 9.9.9"
    assert cli_executor.probe_cli_version("fakecli") == "fakecli 9.9.9"
    assert calls["n"] == 1  # cached after the first probe

    cli_executor._CLI_VERSION_CACHE.clear()

    def _boom(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr("subprocess.run", _boom)
    assert cli_executor.probe_cli_version("missing") is None
    # Failure is cached too — no per-run re-probe of a missing binary.
    monkeypatch.setattr("subprocess.run", _fake_run)
    assert cli_executor.probe_cli_version("missing") is None
    cli_executor._CLI_VERSION_CACHE.clear()
