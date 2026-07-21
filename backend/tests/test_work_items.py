"""
tests/test_work_items.py — S0 substrate slice 2: WorkItem API.

Covers create/list/get/patch, the validated state machine (closed_at stamped on
done/dropped, cleared on reopen), the append-only decisions list, work-item
attachment on task/session create, and owner scoping.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
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


@pytest.fixture
def project_id(client) -> str:
    return client.post("/api/projects", json={"name": "P"}).json()["id"]


def _create(client, project_id, **over):
    body = {"title": "Fix restore", "objective": "restore green twice", **over}
    resp = client.post(f"/api/projects/{project_id}/work-items", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_list_get(client, project_id):
    wi = _create(client, project_id, kind="incident", risk="high",
                 next_action="rerun verify")
    assert wi["state"] == "open"
    assert wi["kind"] == "incident"
    assert wi["closed_at"] is None

    listed = client.get(f"/api/projects/{project_id}/work-items").json()
    assert [w["id"] for w in listed] == [wi["id"]]
    only_open = client.get(f"/api/projects/{project_id}/work-items?state=open").json()
    assert len(only_open) == 1
    assert client.get(
        f"/api/projects/{project_id}/work-items?state=done"
    ).json() == []

    got = client.get(f"/api/work-items/{wi['id']}").json()
    assert got["title"] == "Fix restore"


def test_state_machine_stamps_and_clears_closed_at(client, project_id):
    wi = _create(client, project_id)
    item_id = wi["id"]

    r = client.patch(f"/api/work-items/{item_id}", json={"state": "in_progress"})
    assert r.status_code == 200 and r.json()["closed_at"] is None

    r = client.patch(f"/api/work-items/{item_id}", json={"state": "done"})
    assert r.json()["closed_at"] is not None

    # done → in_progress is not a legal edge; reopen is.
    assert client.patch(
        f"/api/work-items/{item_id}", json={"state": "in_progress"}
    ).status_code == 400
    r = client.patch(f"/api/work-items/{item_id}", json={"state": "reopened"})
    assert r.status_code == 200 and r.json()["closed_at"] is None

    # Unknown states are rejected by the schema itself.
    assert client.patch(
        f"/api/work-items/{item_id}", json={"state": "finished"}
    ).status_code == 422


def test_api_cannot_push_work_back_into_proposed(client, project_id):
    """`proposed` is the P-0078 planner's entry state: only the planner mints one, and
    the accept/reject edges lead out of it. No edge leads *in*, so an operator can
    never demote confirmed work back into the proposal queue."""
    wi = _create(client, project_id)
    assert client.patch(
        f"/api/work-items/{wi['id']}", json={"state": "proposed"}
    ).status_code == 400


def test_invalid_transition_open_to_reopened(client, project_id):
    wi = _create(client, project_id)
    resp = client.patch(f"/api/work-items/{wi['id']}", json={"state": "reopened"})
    assert resp.status_code == 400
    assert "invalid state transition" in resp.json()["detail"]


def test_add_decision_appends(client, project_id):
    wi = _create(client, project_id)
    r = client.patch(
        f"/api/work-items/{wi['id']}",
        json={"add_decision": "use alembic chain", "decision_actor": "mock"},
    ).json()
    r = client.patch(f"/api/work-items/{wi['id']}", json={"add_decision": "ship"}).json()
    decisions = r["decisions"]
    assert [d["text"] for d in decisions] == ["use alembic chain", "ship"]
    assert decisions[0]["actor"] == "mock"
    assert decisions[1]["actor"] == "human"
    assert all(d["ts"] for d in decisions)


def test_patch_fields(client, project_id):
    wi = _create(client, project_id)
    r = client.patch(
        f"/api/work-items/{wi['id']}",
        json={"title": "  New title  ", "next_action": "do X", "risk": "medium"},
    ).json()
    assert r["title"] == "New title"
    assert r["next_action"] == "do X"
    assert r["risk"] == "medium"
    assert client.patch(
        f"/api/work-items/{wi['id']}", json={"risk": "extreme"}
    ).status_code == 422
    assert client.patch(
        f"/api/work-items/{wi['id']}", json={"title": "   "}
    ).status_code == 400


def test_parent_must_share_project(client, project_id):
    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    parent = _create(client, other)
    resp = client.post(
        f"/api/projects/{project_id}/work-items",
        json={"title": "child", "parent_id": parent["id"]},
    )
    assert resp.status_code == 404


def test_task_create_attaches_work_item(client, project_id):
    wi = _create(client, project_id)
    task = client.post(
        "/api/tasks",
        json={"name": "t", "project_id": project_id, "work_item_id": wi["id"]},
    ).json()
    assert task["work_item_id"] == wi["id"]

    # A work item in a different project than the task resolves to 404.
    resp = client.post("/api/tasks", json={"name": "t2", "work_item_id": wi["id"]})
    assert resp.status_code == 404


def test_owner_scoping(client, project_id):
    import app.main as main
    from app.db import get_db
    from app.models import Owner, Project, WorkItem

    async def _seed_foreign():
        gen = main.app.dependency_overrides[get_db]()
        db = await anext(gen)
        db.add(Owner(id="other", label="Other"))
        db.add(Project(id="theirs", owner_id="other", name="Theirs"))
        wi = WorkItem(owner_id="other", project_id="theirs", title="secret")
        db.add(wi)
        await db.flush()
        wid = wi.id
        await db.commit()
        await gen.aclose()
        return wid

    foreign_id = asyncio.get_event_loop().run_until_complete(_seed_foreign())
    assert client.get("/api/projects/theirs/work-items").status_code == 404
    assert client.post(
        "/api/projects/theirs/work-items", json={"title": "x"}
    ).status_code == 404
    assert client.get(f"/api/work-items/{foreign_id}").status_code == 404
    assert client.patch(
        f"/api/work-items/{foreign_id}", json={"title": "hijack"}
    ).status_code == 404
