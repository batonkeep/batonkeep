"""
tests/test_projects_substrate.py — S0 substrate slice 1: schema + migration + seams.

Covers the migration chain (new tables at head; a pre-substrate DB upgrades
losslessly with every row attached to the per-owner default Project; re-run is
idempotent) and the API seams (default-Project resolution on task/session
create, ownership fencing on explicit ids, project_id filters, owner scoping).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

PRE_SUBSTRATE_REV = "a2b3c4d5e6f7"  # last revision before the projects substrate


# ── Migration chain ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_settings_cache():
    """Migration tests repoint DATABASE_URL; re-converge the Settings cache after
    (same pattern as test_db_migrations)."""
    yield
    from app.config import get_settings

    get_settings.cache_clear()
    get_settings()


def _use_db(monkeypatch, url_path: str) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{url_path}")
    get_settings.cache_clear()


def _sync_engine(url_path: str):
    return create_engine(f"sqlite:///{url_path}")


@pytest.mark.asyncio
async def test_fresh_db_has_substrate_tables(tmp_path, monkeypatch):
    import app.db as db

    path = f"{tmp_path}/fresh.db"
    _use_db(monkeypatch, path)
    await db.init_db()

    eng = _sync_engine(path)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    assert {"projects", "work_items", "context_sources",
            "context_receipts", "evidence"} <= tables
    task_cols = {c["name"] for c in insp.get_columns("tasks")}
    assert {"project_id", "work_item_id"} <= task_cols
    run_cols = {c["name"] for c in insp.get_columns("runs")}
    assert {"project_id", "work_item_id"} <= run_cols
    sess_cols = {c["name"] for c in insp.get_columns("sessions")}
    assert {"project_id", "work_item_id"} <= sess_cols
    rd_cols = {c["name"] for c in insp.get_columns("routing_decisions")}
    assert {"project_id", "work_item_kind"} <= rd_cols
    eng.dispose()


def _build_pre_substrate_db_with_rows(path: str) -> None:
    """Upgrade a fresh DB to the last pre-substrate revision and seed rows."""
    import app.db as db
    from alembic import command

    command.upgrade(db._alembic_config(), PRE_SUBSTRATE_REV)
    eng = _sync_engine(path)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO owners (id, label) VALUES ('local', 'Me')"))
        conn.execute(text(
            "INSERT INTO tasks (id, owner_id, name, prompt_template, schedule_kind,"
            " timezone, want_markdown, want_json, enabled, exec_policy)"
            " VALUES (1, 'local', 'T1', 'p', 'none', 'UTC', 1, 0, 1, 'confirmation')"
        ))
        conn.execute(text(
            "INSERT INTO runs (id, owner_id, task_id, trigger, status, retry_count,"
            " overflow_used, tokens_in, tokens_out, cost_usd, subagents, tool_calls)"
            " VALUES (1, 'local', 1, 'manual', 'succeeded', 0, 0, 0, 0, 0.0, 0, 0)"
        ))
        conn.execute(text(
            "INSERT INTO sessions (id, owner_id, title, workspace_path, preview_token,"
            " status, confidential, exec_policy)"
            " VALUES ('s1', 'local', 'S1', '/tmp/ws', 'tok', 'active', 0, 'confirmation')"
        ))
    eng.dispose()


@pytest.mark.asyncio
async def test_pre_substrate_db_backfills_default_project(tmp_path, monkeypatch):
    """The v0.6.0-shaped upgrade path: rows survive and land in the default Project."""
    import app.db as db

    path = f"{tmp_path}/legacy.db"
    _use_db(monkeypatch, path)
    _build_pre_substrate_db_with_rows(path)

    await db.init_db()  # upgrade to head, including the backfill revision

    eng = _sync_engine(path)
    with eng.connect() as conn:
        defaults = conn.execute(text(
            "SELECT id, name FROM projects WHERE owner_id='local' AND is_default=1"
        )).fetchall()
        assert len(defaults) == 1
        default_id, default_name = defaults[0]
        assert default_name == "Personal workspace"
        assert conn.execute(text("SELECT project_id FROM tasks WHERE id=1")).scalar() \
            == default_id
        assert conn.execute(text("SELECT project_id FROM runs WHERE id=1")).scalar() \
            == default_id
        assert conn.execute(text("SELECT project_id FROM sessions WHERE id='s1'")).scalar() \
            == default_id
        # Nothing was lost.
        assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM runs")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar() == 1
    eng.dispose()


@pytest.mark.asyncio
async def test_backfill_is_idempotent(tmp_path, monkeypatch):
    """A second init_db must not create a second default Project or re-point rows."""
    import app.db as db

    path = f"{tmp_path}/idem.db"
    _use_db(monkeypatch, path)
    _build_pre_substrate_db_with_rows(path)
    await db.init_db()

    eng = _sync_engine(path)
    with eng.connect() as conn:
        first_default = conn.execute(text(
            "SELECT id FROM projects WHERE owner_id='local' AND is_default=1"
        )).scalar()

    # Re-running all migrations from the backfill revision must converge, not duplicate.
    from alembic import command

    command.downgrade(db._alembic_config(), "9d3f2c8b7a10")
    command.upgrade(db._alembic_config(), "head")

    with eng.connect() as conn:
        defaults = conn.execute(text(
            "SELECT id FROM projects WHERE owner_id='local' AND is_default=1"
        )).fetchall()
        assert len(defaults) == 1
        assert defaults[0][0] == first_default
    eng.dispose()


# ── API seams ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    """TestClient over a fresh sqlite DB with the local owner seeded (metadata-built,
    like the other API tests — the migration tests above cover the alembic path)."""
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
        # No context manager: entering TestClient runs the lifespan (init_db
        # against the real DATABASE_URL) — same pattern as the other API tests.
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_projects_list_ensures_default(client):
    projects = client.get("/api/projects").json()
    assert len(projects) == 1
    assert projects[0]["name"] == "Personal workspace"
    assert projects[0]["is_default"] is True


def test_task_create_resolves_default_project(client):
    task = client.post("/api/tasks", json={"name": "t"}).json()
    assert task["project_id"]
    default = client.get("/api/projects").json()[0]
    assert task["project_id"] == default["id"]


def test_task_create_with_explicit_project(client):
    project = client.post("/api/projects", json={"name": "Infra"}).json()
    assert project["is_default"] is False

    task = client.post("/api/tasks", json={"name": "t", "project_id": project["id"]}).json()
    assert task["project_id"] == project["id"]

    # The filter narrows to the explicit project only.
    in_project = client.get(f"/api/tasks?project_id={project['id']}").json()
    assert [t["id"] for t in in_project] == [task["id"]]


def test_task_create_rejects_unknown_or_foreign_project(client):
    resp = client.post("/api/tasks", json={"name": "t", "project_id": "nope"})
    assert resp.status_code == 404

    # A project belonging to another owner is indistinguishable from absent.
    import app.main as main
    from app.db import get_db
    from app.models import Owner, Project

    async def _seed_foreign():
        gen = main.app.dependency_overrides[get_db]()
        db = await anext(gen)
        db.add(Owner(id="other", label="Other"))
        db.add(Project(id="foreignproj", owner_id="other", name="Theirs"))
        await db.commit()
        await gen.aclose()

    asyncio.get_event_loop().run_until_complete(_seed_foreign())
    resp = client.post("/api/tasks", json={"name": "t", "project_id": "foreignproj"})
    assert resp.status_code == 404
    # And it never appears in the owner's project list.
    assert all(p["id"] != "foreignproj" for p in client.get("/api/projects").json())


def test_get_foreign_project_404s(client):
    assert client.get("/api/projects/doesnotexist").status_code == 404


@pytest.mark.asyncio
async def test_run_inherits_task_project(tmp_path):
    """enqueue_run copies the task's project/work-item onto the run row."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.orchestrator as orch_mod
    from app.db import Base
    from app.models import Owner, Project, Task

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/enq.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as db:
        db.add(Owner(id="local", label="Test"))
        db.add(Project(id="proj1", owner_id="local", name="P1"))
        task = Task(
            owner_id="local", project_id="proj1", name="t", prompt_template="p",
            routing={"strategy": "fixed", "candidates": ["mock"],
                     "failover": False, "max_attempts": 1},
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    orig = orch_mod.AsyncSessionLocal
    orch_mod.AsyncSessionLocal = Maker
    try:
        run = await orch_mod.enqueue_run(task_id, trigger="test")
        assert run.project_id == "proj1"
        # Don't leave the background execution dangling against the tmp DB.
        bg = orch_mod._cancel_handles.get(run.id)
        if bg:
            bg.cancel()
            try:
                await bg
            except BaseException:
                pass  # cancelled (or already finished) — either is fine here
    finally:
        orch_mod.AsyncSessionLocal = orig
        await engine.dispose()


# ── Managed context roots (S0.4) ─────────────────────────────────────────────

def test_create_project_with_managed_root(client, tmp_path, monkeypatch):
    """create_root builds projects/<id>/context on the data volume — git-init'd,
    starter README + manifest, and the bootstrap source imported immediately."""
    import os
    import subprocess

    import app.main as main

    monkeypatch.setattr(main.settings, "projects_dir", str(tmp_path / "proots"))

    p = client.post(
        "/api/projects",
        json={"name": "Homelab", "create_root": True, "description": "Estate docs"},
    ).json()
    root = p["root_path"]
    assert root == str(tmp_path / "proots" / p["id"] / "context")
    assert os.path.isfile(os.path.join(root, "batonkeep.yaml"))
    with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    assert readme.startswith("# Homelab")
    assert "Estate docs" in readme

    out = subprocess.run(
        ["git", "-C", root, "log", "--oneline"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "managed context root" in out.stdout

    # The starter manifest declares README.md; it shows up as a hashed bootstrap
    # source without a separate import call.
    sources = client.get(f"/api/projects/{p['id']}/context-sources").json()
    assert [s["rel_path"] for s in sources] == ["README.md"]
    assert sources[0]["bootstrap_order"] == 1
    assert sources[0]["last_revision"]


def test_create_project_root_choice_is_exclusive(client):
    resp = client.post(
        "/api/projects",
        json={"name": "X", "create_root": True, "root_path": "/somewhere"},
    )
    assert resp.status_code == 422


def test_create_project_managed_root_unwritable_is_409(client, tmp_path, monkeypatch):
    """A base the backend can't write turns into a clean 409 with nothing
    half-created — same posture as canonical writes on an unwritable root."""
    import os

    import app.main as main

    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits; the write would succeed")

    base = tmp_path / "ro"
    base.mkdir()
    base.chmod(0o555)
    monkeypatch.setattr(main.settings, "projects_dir", str(base))
    try:
        resp = client.post("/api/projects", json={"name": "X", "create_root": True})
        assert resp.status_code == 409
        assert "not writable" in resp.json()["detail"]
        assert all(pr["name"] != "X" for pr in client.get("/api/projects").json())
    finally:
        base.chmod(0o755)
