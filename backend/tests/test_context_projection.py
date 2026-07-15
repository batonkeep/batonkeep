"""
tests/test_context_projection.py — S0 substrate slice 2: manifest, hashing,
projection, ledger, receipts.

Covers the manifest parser (valid/unknown-keys/invalid), revision hashing
(file sha256, dir merkle, git HEAD), source import/refresh via the API, the
read-only projection + deterministic WORKITEM.md ledger, receipt persistence
(including through a real mock-provider run), and owner scoping on every new
route.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.project_context import (
    ManifestError,
    compute_revision,
    detect_kind,
    parse_manifest_text,
    project_for_execution,
)
from app.work_ledger import render_ledger, sha256_text

MANIFEST = """\
apiVersion: batonkeep.dev/v1alpha1
kind: Project
context:
  bootstrap:
    - README.md
    - docs/plan.md
  domains:
    runbooks: runbooks
  evidence: evidence
"""


# ── Manifest parsing ──────────────────────────────────────────────────────────

def test_parse_manifest_valid():
    m = parse_manifest_text(MANIFEST)
    assert m.bootstrap == ["README.md", "docs/plan.md"]
    assert m.domains == {"runbooks": "runbooks"}
    assert m.evidence_dir == "evidence"
    assert m.warnings == []


def test_parse_manifest_unknown_keys_warn_not_fail():
    m = parse_manifest_text(
        MANIFEST + "extra: 1\ncontext2: {}\n"
    )
    assert any("extra" in w for w in m.warnings)
    m2 = parse_manifest_text(
        "apiVersion: batonkeep.dev/v1alpha1\nkind: Project\n"
        "context:\n  bootstrap: [a.md]\n  future_key: x\n"
    )
    assert any("future_key" in w for w in m2.warnings)
    assert m2.bootstrap == ["a.md"]


def test_parse_manifest_duplicate_bootstrap_warns():
    m = parse_manifest_text(
        "apiVersion: batonkeep.dev/v1alpha1\nkind: Project\n"
        "context:\n  bootstrap: [a.md, a.md]\n"
    )
    assert m.bootstrap == ["a.md"]
    assert any("duplicate" in w for w in m.warnings)


@pytest.mark.parametrize("text", [
    "apiVersion: wrong/v1\nkind: Project\n",
    "apiVersion: batonkeep.dev/v1alpha1\nkind: Task\n",
    "- just\n- a list\n",
    "apiVersion: batonkeep.dev/v1alpha1\nkind: Project\ncontext: [not, a, map]\n",
    "apiVersion: batonkeep.dev/v1alpha1\nkind: Project\ncontext:\n  bootstrap: [/etc/passwd]\n",
    "apiVersion: batonkeep.dev/v1alpha1\nkind: Project\ncontext:\n  bootstrap: [../escape]\n",
])
def test_parse_manifest_invalid(text):
    with pytest.raises(ManifestError):
        parse_manifest_text(text)


# ── Revision hashing ──────────────────────────────────────────────────────────

def test_file_revision_is_content_sha256(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("hello")
    rev = compute_revision(str(tmp_path), "a.md", "file")
    assert rev == hashlib.sha256(b"hello").hexdigest()


def test_dir_revision_merkle_stable_and_content_sensitive(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "b.md").write_text("bbb")
    (d / "a.md").write_text("aaa")
    rev1 = compute_revision(str(tmp_path), "docs", "dir")
    rev2 = compute_revision(str(tmp_path), "docs", "dir")
    assert rev1 == rev2
    (d / "c.md").write_text("ccc")
    assert compute_revision(str(tmp_path), "docs", "dir") != rev1


def test_dir_revision_excludes_git_internals(tmp_path):
    d = tmp_path / "src"
    (d / ".git").mkdir(parents=True)
    (d / "x.py").write_text("x = 1")
    before = compute_revision(str(tmp_path), "src", "dir")
    (d / ".git" / "index").write_text("churn")
    assert compute_revision(str(tmp_path), "src", "dir") == before


def test_git_revision_matches_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "f").write_text("1")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True, env=env)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True, env=env).stdout.strip()
    assert detect_kind(str(tmp_path), "repo") == "git"
    assert compute_revision(str(tmp_path), "repo", "git") == head


def test_missing_source_revision_is_none(tmp_path):
    assert compute_revision(str(tmp_path), "nope.md", "file") is None


# ── Ledger determinism ────────────────────────────────────────────────────────

def _work_item_stub(**over):
    from app.models import WorkItem

    defaults = dict(
        owner_id="local", project_id="p", kind="task", state="in_progress",
        title="Fix the flaky restore", objective="Restore passes twice in a row",
        next_action="Re-run the restore verify", risk="low",
        decisions=[{"ts": "2026-07-15T00:00:00+00:00", "actor": "human", "text": "go"}],
    )
    defaults.update(over)
    return WorkItem(**defaults)


def test_ledger_is_byte_stable_given_equal_inputs():
    a = render_ledger(project_name="P", work_item=_work_item_stub(),
                      changed_files=["a.py"], evidence_index=[])
    b = render_ledger(project_name="P", work_item=_work_item_stub(),
                      changed_files=["a.py"], evidence_index=[])
    assert a == b
    assert sha256_text(a) == sha256_text(b)


def test_ledger_changes_when_fields_change():
    a = render_ledger(project_name="P", work_item=_work_item_stub())
    b = render_ledger(project_name="P",
                      work_item=_work_item_stub(next_action="Ship it"))
    assert sha256_text(a) != sha256_text(b)


def test_ledger_without_work_item_is_deterministic():
    a = render_ledger(project_name="P", work_item=None)
    assert a == render_ledger(project_name="P", work_item=None)
    assert "No work item" in a


# ── API: import, refresh, receipts, owner scoping ─────────────────────────────

@pytest.fixture
def client(tmp_path):
    """TestClient over a fresh sqlite DB (metadata-built) — the slice-1 pattern."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.main as main
    from app.db import Base, get_db
    from app.models import Owner

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


def _make_root(tmp_path) -> str:
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "runbooks").mkdir()
    (root / "README.md").write_text("# readme")
    (root / "docs" / "plan.md").write_text("plan")
    (root / "runbooks" / "restore.md").write_text("steps")
    (root / "batonkeep.yaml").write_text(MANIFEST)
    return str(root)


def test_import_sources_from_manifest(client, tmp_path):
    root = _make_root(tmp_path)
    project = client.post("/api/projects", json={"name": "P", "root_path": root}).json()

    out = client.post(f"/api/projects/{project['id']}/context-sources", json={})
    assert out.status_code == 201
    body = out.json()
    assert body["warnings"] == []
    by_rel = {s["rel_path"]: s for s in body["sources"]}
    assert by_rel["README.md"]["bootstrap_order"] == 1
    assert by_rel["docs/plan.md"]["bootstrap_order"] == 2
    assert by_rel["runbooks"]["domain"] == "runbooks"
    assert by_rel["runbooks"]["kind"] == "dir"
    # Import refreshes hashes inline — freshness visible without a second call.
    assert by_rel["README.md"]["last_revision"] == hashlib.sha256(b"# readme").hexdigest()

    # Idempotent re-import: same three sources, not six.
    again = client.post(f"/api/projects/{project['id']}/context-sources", json={}).json()
    assert len(again["sources"]) == 3
    listed = client.get(f"/api/projects/{project['id']}/context-sources").json()
    assert len(listed) == 3
    # Bootstrap sources listed first, in order.
    assert [s["rel_path"] for s in listed[:2]] == ["README.md", "docs/plan.md"]


def test_import_without_manifest_400s(client, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    project = client.post("/api/projects", json={"name": "P", "root_path": str(root)}).json()
    resp = client.post(f"/api/projects/{project['id']}/context-sources", json={})
    assert resp.status_code == 400
    assert "manifest" in resp.json()["detail"].lower()


def test_declare_explicit_source_and_refresh(client, tmp_path):
    root = _make_root(tmp_path)
    project = client.post("/api/projects", json={"name": "P", "root_path": root}).json()

    out = client.post(
        f"/api/projects/{project['id']}/context-sources",
        json={"rel_path": "docs/plan.md", "bootstrap_order": 9},
    )
    assert out.status_code == 201
    source = out.json()["sources"][0]
    assert source["kind"] == "file"
    old_rev = source["last_revision"]
    assert old_rev == hashlib.sha256(b"plan").hexdigest()

    # Content changes; refresh re-hashes and updates freshness.
    (tmp_path / "root" / "docs" / "plan.md").write_text("plan v2")
    refreshed = client.post(f"/api/projects/{project['id']}/context/refresh").json()
    assert refreshed[0]["last_revision"] == hashlib.sha256(b"plan v2").hexdigest()
    assert refreshed[0]["last_checked_at"] is not None


def test_declare_traversal_rel_path_400s(client, tmp_path):
    root = _make_root(tmp_path)
    project = client.post("/api/projects", json={"name": "P", "root_path": root}).json()
    resp = client.post(
        f"/api/projects/{project['id']}/context-sources",
        json={"rel_path": "../outside"},
    )
    assert resp.status_code == 400


def test_context_routes_owner_scoped(client):
    """Every new route 404s for foreign/unknown projects — indistinguishable."""
    import app.main as main
    from app.db import get_db
    from app.models import Owner, Project

    async def _seed_foreign():
        gen = main.app.dependency_overrides[get_db]()
        db = await anext(gen)
        db.add(Owner(id="other", label="Other"))
        db.add(Project(id="theirs", owner_id="other", name="Theirs"))
        await db.commit()
        await gen.aclose()

    asyncio.get_event_loop().run_until_complete(_seed_foreign())
    for pid in ("theirs", "absent"):
        assert client.get(f"/api/projects/{pid}/context-sources").status_code == 404
        assert client.post(
            f"/api/projects/{pid}/context-sources", json={}
        ).status_code == 404
        assert client.post(f"/api/projects/{pid}/context/refresh").status_code == 404
    assert client.get("/api/runs/999/receipt").status_code == 404
    assert client.get("/api/sessions/nope/turns/1/receipt").status_code == 404


# ── Projection + receipts ─────────────────────────────────────────────────────

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


async def _seed_project(Maker, root: str, *, with_work_item=True):
    from app.models import ContextSource, Project, WorkItem

    async with Maker() as db:
        project = Project(id="p1", owner_id="local", name="P1", root_path=root)
        db.add(project)
        db.add(ContextSource(owner_id="local", project_id="p1", kind="file",
                             rel_path="README.md", bootstrap_order=1))
        db.add(ContextSource(owner_id="local", project_id="p1", kind="dir",
                             rel_path="runbooks", domain="runbooks"))
        wi_id = None
        if with_work_item:
            wi = WorkItem(
                owner_id="local", project_id="p1", title="W", state="in_progress",
                objective="obj", next_action="next",
                decisions=[{"ts": "2026-07-15T00:00:00+00:00", "actor": "human",
                            "text": "approved plan"}],
            )
            db.add(wi)
            await db.flush()
            wi_id = wi.id
        await db.commit()
    return wi_id


async def test_projection_materializes_read_only_and_persists_receipt(proj_db, tmp_path):
    root = _make_root(tmp_path)
    wi_id = await _seed_project(proj_db, root)
    workdir = tmp_path / "work"
    workdir.mkdir()

    async with proj_db() as db:
        receipt = await project_for_execution(
            db, owner_id="local", project_id="p1", work_item_id=wi_id,
            workdir=str(workdir), run_id=None,
        )
    assert receipt is not None and receipt.id is not None
    assert receipt.projection_version == "proj-v1"
    assert [s["rel_path"] for s in receipt.sources] == ["README.md", "runbooks"]
    assert all(s["revision"] for s in receipt.sources)
    assert receipt.approx_bytes > 0
    assert receipt.exclusions is None

    ctx = workdir / "context"
    assert (ctx / "README.md").read_text() == "# readme"
    assert (ctx / "runbooks" / "restore.md").read_text() == "steps"
    # Read-only files (0444): the agent's copy can't be silently rewritten.
    assert (os.stat(ctx / "README.md").st_mode & 0o777) == 0o444
    ledger = workdir / "WORKITEM.md"
    text = ledger.read_text()
    assert "obj" in text and "next" in text and "approved plan" in text
    assert (os.stat(ledger).st_mode & 0o777) == 0o444
    from app.work_ledger import sha256_text as sha
    assert receipt.ledger_sha == sha(text)


async def test_projection_receipt_deterministic_minus_ts(proj_db, tmp_path):
    root = _make_root(tmp_path)
    wi_id = await _seed_project(proj_db, root)
    receipts = []
    for name in ("w1", "w2"):
        wd = tmp_path / name
        wd.mkdir()
        async with proj_db() as db:
            receipts.append(await project_for_execution(
                db, owner_id="local", project_id="p1", work_item_id=wi_id,
                workdir=str(wd),
            ))
    a, b = receipts
    # Same sources + fields → same ledger_sha, same receipt minus id/ts.
    assert a.ledger_sha == b.ledger_sha
    assert a.sources == b.sources
    assert a.exclusions == b.exclusions
    assert a.approx_bytes == b.approx_bytes


async def test_projection_reruns_replace_previous(proj_db, tmp_path):
    """A second projection into the same workspace (session lane) replaces the
    read-only context/ and WORKITEM.md rather than failing on permissions."""
    root = _make_root(tmp_path)
    wi_id = await _seed_project(proj_db, root)
    wd = tmp_path / "ws"
    wd.mkdir()
    for _ in range(2):
        async with proj_db() as db:
            receipt = await project_for_execution(
                db, owner_id="local", project_id="p1", work_item_id=wi_id,
                workdir=str(wd),
            )
    assert receipt is not None
    assert (wd / "context" / "README.md").is_file()


async def test_projection_budget_and_missing_exclusions(proj_db, tmp_path):
    import app.project_context as pc

    root = _make_root(tmp_path)
    await _seed_project(proj_db, root, with_work_item=False)
    async with proj_db() as db:
        from app.models import ContextSource
        db.add(ContextSource(owner_id="local", project_id="p1", kind="file",
                             rel_path="gone.md"))
        await db.commit()

    wd = tmp_path / "wbudget"
    wd.mkdir()
    old = pc._settings.context_projection_max_bytes
    pc._settings.__dict__["context_projection_max_bytes"] = 4  # below any source
    try:
        async with proj_db() as db:
            receipt = await project_for_execution(
                db, owner_id="local", project_id="p1", workdir=str(wd),
            )
    finally:
        pc._settings.__dict__["context_projection_max_bytes"] = old
    reasons = {e["rel_path"]: e["reason"] for e in receipt.exclusions}
    assert reasons["gone.md"] == "missing"
    assert reasons["README.md"] == "budget"
    assert receipt.sources == []


async def test_projection_without_project_returns_none(proj_db, tmp_path):
    async with proj_db() as db:
        assert await project_for_execution(
            db, owner_id="local", project_id=None, workdir=str(tmp_path),
        ) is None


async def test_run_execution_persists_receipt(proj_db, tmp_path):
    """Task lane end-to-end on the mock executor: the receipt exists and points
    at the run — persisted by the orchestrator before the executor started."""
    import app.orchestrator as orch
    from app.models import ContextReceipt, Task
    from tests.test_run_single_execution import CountingExecutor

    root = _make_root(tmp_path)
    wi_id = await _seed_project(proj_db, root)

    async with proj_db() as db:
        task = Task(
            owner_id="local", project_id="p1", work_item_id=wi_id, name="t",
            prompt_template="p", params={},
            routing={"strategy": "fixed", "candidates": ["mock"],
                     "failover": False, "max_attempts": 1},
            want_markdown=True, want_json=False,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    orig_sl = orch.AsyncSessionLocal
    orig_get = orch.get_executor
    orch.AsyncSessionLocal = proj_db
    orch.get_executor = lambda iid: CountingExecutor(iid)
    orch._settings.__dict__["outputs_dir"] = str(tmp_path / "out")
    orch._settings.__dict__["work_dir"] = str(tmp_path / "workroot")
    try:
        run = await orch.enqueue_run(task_id, trigger="test")
        bg = orch._cancel_handles.get(run.id)
        if bg:
            await asyncio.wait_for(asyncio.shield(bg), timeout=10)
        async with proj_db() as db:
            receipt = (await db.execute(
                select(ContextReceipt).where(ContextReceipt.run_id == run.id)
            )).scalars().one()
            assert receipt.work_item_id == wi_id
            assert receipt.ledger_sha
            assert [s["rel_path"] for s in receipt.sources] == ["README.md", "runbooks"]
    finally:
        orch.AsyncSessionLocal = orig_sl
        orch.get_executor = orig_get


def test_receipt_routes_return_latest(client, tmp_path):
    """GET /api/runs/{id}/receipt and the turn variant serve the persisted rows."""
    import app.main as main
    from app.db import get_db
    from app.models import ContextReceipt, Project, Run, Session, SessionTurn, Task

    async def _seed():
        gen = main.app.dependency_overrides[get_db]()
        db = await anext(gen)
        db.add(Project(id="p1", owner_id="local", name="P"))
        task = Task(owner_id="local", project_id="p1", name="t", prompt_template="p")
        db.add(task)
        await db.flush()
        run = Run(owner_id="local", task_id=task.id, project_id="p1",
                  trigger="manual", status="succeeded")
        session = Session(id="s1", owner_id="local", project_id="p1", title="S",
                          workspace_path="/tmp/x", status="active")
        db.add_all([run, session])
        await db.flush()
        turn = SessionTurn(session_id="s1", owner_id="local", seq=1, prompt="hi",
                           status="succeeded")
        db.add(turn)
        await db.flush()
        db.add(ContextReceipt(owner_id="local", project_id="p1", run_id=run.id,
                              projection_version="proj-v1", sources=[],
                              ledger_sha="a" * 64, approx_bytes=1))
        db.add(ContextReceipt(owner_id="local", project_id="p1",
                              session_turn_id=turn.id, projection_version="proj-v1",
                              sources=[], ledger_sha="b" * 64, approx_bytes=1))
        await db.commit()
        ids = (run.id, turn.id)
        await gen.aclose()
        return ids

    run_id, turn_id = asyncio.get_event_loop().run_until_complete(_seed())

    got = client.get(f"/api/runs/{run_id}/receipt")
    assert got.status_code == 200
    assert got.json()["ledger_sha"] == "a" * 64

    got = client.get(f"/api/sessions/s1/turns/{turn_id}/receipt")
    assert got.status_code == 200
    assert got.json()["ledger_sha"] == "b" * 64
