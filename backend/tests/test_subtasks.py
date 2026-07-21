"""tests/test_subtasks.py — P-0069 B2: WorkItem sub-task checklist = output contract
+ grounded progress (pure logic in app/subtasks.py + the propose/confirm API)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import subtasks as st
from app.db import Base, get_db
from app.main import _owner_id, app
from app.models import Owner, Project, WorkItem


class TestChecklistPure:
    def test_make_item_defaults_and_verifiable_flag(self):
        asserted = st.make_item("write the summary")
        assert asserted["status"] == "proposed"
        assert asserted["expected"] is None and asserted["done"] is False
        verifiable = st.make_item("ship report", expected="report.md")
        assert verifiable["expected"] == "report.md"

    def test_make_item_rejects_empty_label(self):
        with pytest.raises(ValueError):
            st.make_item("   ")

    def test_append_proposed_accumulates(self):
        s = st.append_proposed(None, [{"label": "a"}], proposed_by="agy")
        s = st.append_proposed(s, [{"label": "b", "expected": "b.txt"}], proposed_by="agy")
        assert [i["label"] for i in s["items"]] == ["a", "b"]
        assert all(i["status"] == "proposed" for i in s["items"])
        assert s["items"][1]["proposed_by"] == "agy"

    def test_append_proposed_enforces_cap(self):
        many = [{"label": f"i{n}"} for n in range(st.MAX_ITEMS + 1)]
        with pytest.raises(ValueError):
            st.append_proposed(None, many, proposed_by="op")

    def test_set_items_preserves_verification_on_unchanged_target(self):
        s = st.append_proposed(None, [{"label": "x", "expected": "out.md"}], proposed_by="op")
        iid = s["items"][0]["id"]
        # confirm it, then verify against a tree that has out.md
        s = st.set_items(s, [{"id": iid, "label": "x", "expected": "out.md",
                              "status": "confirmed"}])
        s, changed = st.verify(s, {"out.md"})
        assert changed and s["items"][0]["verified"] is True
        # re-confirm with the same target → verification carried forward
        s2 = st.set_items(s, [{"id": iid, "label": "x (renamed)", "expected": "out.md",
                               "status": "confirmed"}])
        assert s2["items"][0]["verified"] is True

    def test_set_items_resets_verification_on_changed_target(self):
        s = st.append_proposed(None, [{"label": "x", "expected": "a.md"}], proposed_by="op")
        iid = s["items"][0]["id"]
        s = st.set_items(s, [{"id": iid, "label": "x", "expected": "a.md",
                              "status": "confirmed"}])
        s, _ = st.verify(s, {"a.md"})
        assert s["items"][0]["verified"] is True
        s2 = st.set_items(s, [{"id": iid, "label": "x", "expected": "b.md",
                               "status": "confirmed"}])
        assert s2["items"][0]["verified"] is False  # target changed → reset

    def test_verify_matches_glob_and_reflects_disappearance(self):
        s = st.append_proposed(
            None, [{"label": "charts", "expected": "charts/*.png"}], proposed_by="op"
        )
        s = st.set_items(s, [{**s["items"][0], "status": "confirmed"}])
        s, changed = st.verify(s, {"charts/q1.png", "readme.md"})
        assert changed and s["items"][0]["verified"] is True
        # artifact reverted away → verification flips back to false
        s, changed = st.verify(s, {"readme.md"})
        assert changed and s["items"][0]["verified"] is False

    def test_verify_ignores_proposed_and_asserted_items(self):
        s = st.append_proposed(
            None,
            [{"label": "unconfirmed", "expected": "x.md"}, {"label": "asserted"}],
            proposed_by="op",
        )
        # neither confirmed → verify is a no-op even though x.md exists
        s2, changed = st.verify(s, {"x.md"})
        assert changed is False

    def test_progress_rollup_grounded(self):
        s = st.append_proposed(
            None,
            [
                {"label": "v1", "expected": "a.md"},
                {"label": "v2", "expected": "b.md"},
                {"label": "asserted-done"},
                {"label": "still-open"},
            ],
            proposed_by="op",
        )
        # confirm all four; mark the asserted one done
        s = st.set_items(s, [
            {**s["items"][0], "status": "confirmed"},
            {**s["items"][1], "status": "confirmed"},
            {**s["items"][2], "status": "confirmed", "done": True},
            {**s["items"][3], "status": "confirmed"},
        ])
        s, _ = st.verify(s, {"a.md"})  # only v1's artifact exists
        p = st.progress(s)
        assert p == {"total": 4, "verified": 1, "claimed": 1, "done": 2, "proposed": 0}

    def test_progress_counts_only_confirmed(self):
        s = st.append_proposed(None, [{"label": "a"}, {"label": "b"}], proposed_by="op")
        p = st.progress(s)
        assert p["total"] == 0 and p["proposed"] == 2

    # ── P-0069 slice C tails ────────────────────────────────────────────────────
    def test_verify_stamps_provenance_on_new_verification(self):
        s = st.append_proposed(None, [{"label": "x", "expected": "out.md"}], proposed_by="op")
        s = st.set_items(s, [{**s["items"][0], "status": "confirmed"}])
        s, changed = st.verify(
            s, {"out.md"}, source={"lane": "session", "ref": "sess-abc", "seq": 3}
        )
        vb = s["items"][0]["verified_by"]
        assert changed and vb["lane"] == "session" and vb["ref"] == "sess-abc"
        assert vb["seq"] == 3 and "at" in vb

    def test_verify_provenance_disambiguates_same_wi_across_lanes(self):
        # Two verifiable items, each grounded by a different unit of work whose per-lane
        # seq both start at 1 — provenance ref is what tells them apart WorkItem-wide.
        s = st.append_proposed(
            None,
            [{"label": "a", "expected": "a.md"}, {"label": "b", "expected": "b.md"}],
            proposed_by="op",
        )
        s = st.set_items(s, [
            {**s["items"][0], "status": "confirmed"},
            {**s["items"][1], "status": "confirmed"},
        ])
        s, _ = st.verify(s, {"a.md"}, source={"lane": "session", "ref": "sess-1", "seq": 1})
        s, _ = st.verify(s, {"a.md", "b.md"}, source={"lane": "task", "ref": "run:9", "seq": 1})
        refs = {i["label"]: i["verified_by"]["ref"] for i in s["items"]}
        assert refs == {"a": "sess-1", "b": "run:9"}

    def test_verify_clears_provenance_on_disappearance(self):
        s = st.append_proposed(None, [{"label": "x", "expected": "out.md"}], proposed_by="op")
        s = st.set_items(s, [{**s["items"][0], "status": "confirmed"}])
        s, _ = st.verify(s, {"out.md"}, source={"lane": "task", "ref": "run:1"})
        assert s["items"][0]["verified_by"] is not None
        s, changed = st.verify(s, set(), source={"lane": "task", "ref": "run:2"})
        assert changed and s["items"][0]["verified_by"] is None

    def test_set_items_carries_provenance_forward(self):
        s = st.append_proposed(None, [{"label": "x", "expected": "out.md"}], proposed_by="op")
        iid = s["items"][0]["id"]
        s = st.set_items(s, [{"id": iid, "label": "x", "expected": "out.md",
                              "status": "confirmed"}])
        s, _ = st.verify(s, {"out.md"}, source={"lane": "session", "ref": "sess-1"})
        s2 = st.set_items(s, [{"id": iid, "label": "x2", "expected": "out.md",
                               "status": "confirmed"}])
        assert s2["items"][0]["verified_by"]["ref"] == "sess-1"

    def test_unverified_verifiable_is_the_open_contract(self):
        s = st.append_proposed(
            None,
            [
                {"label": "made", "expected": "a.md"},
                {"label": "missing", "expected": "b.md"},
                {"label": "asserted"},
            ],
            proposed_by="op",
        )
        s = st.set_items(s, [
            {**s["items"][0], "status": "confirmed"},
            {**s["items"][1], "status": "confirmed"},
            {**s["items"][2], "status": "confirmed"},
        ])
        s, _ = st.verify(s, {"a.md"})  # only the first landed
        missing = st.unverified_verifiable(s)
        # asserted item (no expected) is never "outputs_missing"; verified one drops out
        assert [m["label"] for m in missing] == ["missing"]
        assert missing[0]["expected"] == "b.md"


class TestSubtaskApi:
    def _client(self, tmp_path):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add(Owner(id="local", label="T"))
                db.add(Project(id="p1", owner_id="local", name="P"))
                db.add(WorkItem(id=1, owner_id="local", project_id="p1", title="WI"))
                await db.commit()
            return Maker

        Maker = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override
        app.dependency_overrides[_owner_id] = lambda: "local"
        return TestClient(app), engine

    def test_propose_then_confirm_flow(self, tmp_path):


        c, engine = self._client(tmp_path)
        try:
            # agent/operator proposes two items
            r = c.post("/api/work-items/1/subtasks", json={
                "items": [{"label": "ship report", "expected": "report.md"},
                          {"label": "note findings"}],
                "proposed_by": "agy",
            })
            assert r.status_code == 200
            body = r.json()
            assert body["subtask_progress"]["proposed"] == 2
            assert body["subtask_progress"]["total"] == 0
            ids = [i["id"] for i in body["subtasks"]["items"]]

            # operator confirms one, drops the other by omission, edits a label
            r = c.put("/api/work-items/1/subtasks", json={
                "items": [{"id": ids[0], "label": "ship the report",
                           "expected": "report.md", "status": "confirmed"}],
            })
            assert r.status_code == 200
            body = r.json()
            assert len(body["subtasks"]["items"]) == 1
            assert body["subtasks"]["items"][0]["label"] == "ship the report"
            assert body["subtask_progress"] == {
                "total": 1, "verified": 0, "claimed": 0, "done": 0, "proposed": 0
            }
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())

    def test_propose_over_cap_is_400(self, tmp_path):


        c, engine = self._client(tmp_path)
        try:
            r = c.post("/api/work-items/1/subtasks", json={
                "items": [{"label": f"i{n}"} for n in range(st.MAX_ITEMS + 1)],
            })
            assert r.status_code == 400
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())

    def test_unknown_work_item_404(self, tmp_path):


        c, engine = self._client(tmp_path)
        try:
            r = c.post("/api/work-items/999/subtasks", json={"items": [{"label": "x"}]})
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())
