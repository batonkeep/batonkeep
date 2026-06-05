"""
tests/test_templates.py — session task types (P-0010 / D-0011).

A template seeds the session goal + a task-guidance block into SESSION.md, which
the orchestrator injects into every turn's context — so the agent reads the
guidance with no engine change. Ship scope: #1 Summarize + #3 Draft.
"""
from __future__ import annotations

import pytest

from app.sessions import templates as tmpl


class TestTemplateRegistry:
    def test_ships_summarize_and_draft(self):
        ids = {t.id for t in tmpl.list_templates()}
        assert {"summarize", "draft"} <= ids
        # #2 web research is deferred (egress decision); not offered yet.
        assert "research" not in ids

    def test_get_unknown_is_none(self):
        assert tmpl.get_template("nope") is None
        assert tmpl.get_template("summarize") is not None


class TestTemplateSeedsBrief:
    @pytest.mark.asyncio
    async def test_guidance_lands_in_session_md(self, tmp_path):
        from app.sessions import workspace as ws

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        try:
            t = tmpl.get_template("summarize")
            root = await ws.create_workspace(
                "s1", title=t.label, goal=t.goal, guidance=t.guidance,
            )
            brief = ws.read_brief(root)
            assert "## Task guidance" in brief
            assert "document-summarization" in brief
            assert t.goal in brief
            # And it flows into the per-turn context the agent sees.
            ctx = ws.build_turn_context(root, "go")
            assert "## Task guidance" in ctx
        finally:
            ws._settings.__dict__.pop("sessions_dir", None)

    @pytest.mark.asyncio
    async def test_no_template_no_guidance_block(self, tmp_path):
        from app.sessions import workspace as ws

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        try:
            root = await ws.create_workspace("s2", title="Blank", goal="")
            assert "## Task guidance" not in ws.read_brief(root)
        finally:
            ws._settings.__dict__.pop("sessions_dir", None)


class TestTemplateHTTP:
    def test_list_and_create_with_template(self, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner
        from app.main import app, _owner_id
        from app.sessions import workspace as ws

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add(Owner(id="local", label="Me"))
                await db.commit()
            return Maker

        Maker = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)

            templates = c.get("/api/session-templates").json()
            assert {t["id"] for t in templates} >= {"summarize", "draft"}
            assert all({"id", "label", "description"} <= t.keys() for t in templates)

            # Create with a template → goal + title default from it.
            r = c.post("/api/sessions", json={"template": "draft"})
            assert r.status_code == 201, r.text
            s = r.json()
            assert s["title"] == "Draft content"

            # Unknown template rejected.
            assert c.post("/api/sessions", json={"template": "bogus"}).status_code == 400
        finally:
            app.dependency_overrides.clear()
            ws._settings.__dict__.pop("sessions_dir", None)
