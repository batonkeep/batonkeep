"""
tests/test_cf_pages.py — Cloudflare Pages publish connector (D-0009 host connector).

Covers everything verifiable without a live Cloudflare account: encrypted config
round-trip + validation, the wrangler command/URL-parse logic, the deploy
orchestration (with wrangler mocked), and the routes incl. the "token never
leaves the backend" boundary. The real wrangler call needs a live smoke test.
"""
from __future__ import annotations

import os

import pytest

from app.sessions import cf_pages


# ── Pure helpers ──────────────────────────────────────────────────────────────

class TestHelpers:
    def test_deploy_cmd_shape(self):
        cmd = cf_pages._deploy_cmd("/tmp/site", "my-proj", "main")
        assert cmd[:3] == ["wrangler", "pages", "deploy"]
        assert "/tmp/site" in cmd
        assert "--project-name=my-proj" in cmd
        assert "--branch=main" in cmd

    def test_project_create_cmd_shape(self):
        cmd = cf_pages._project_create_cmd("my-proj", "main")
        assert cmd[:4] == ["wrangler", "pages", "project", "create"]
        assert "--production-branch=main" in cmd

    def test_deploy_env_carries_credentials_not_argv(self):
        env = cf_pages._deploy_env("tok-secret", "acct-123")
        assert env["CLOUDFLARE_API_TOKEN"] == "tok-secret"
        assert env["CLOUDFLARE_ACCOUNT_ID"] == "acct-123"
        assert env["WRANGLER_SEND_METRICS"] == "false"

    def test_parse_deploy_url(self):
        out = "✨ Deployment complete! Take a peek over at https://abc123.my-proj.pages.dev\n"
        assert cf_pages._parse_deploy_url(out) == "https://abc123.my-proj.pages.dev"

    def test_parse_deploy_url_none(self):
        assert cf_pages._parse_deploy_url("no url here") is None


# ── Config storage (encrypted credential store) ───────────────────────────────

@pytest.fixture
async def db(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import Base
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cf.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    async with Maker() as session:
        session.add(Owner(id="local", label="Me"))
        await session.commit()
    async with Maker() as session:
        yield session


class TestConfig:
    async def test_roundtrip(self, db):
        assert await cf_pages.get_config(db, "local") is None
        await cf_pages.set_config(db, "local", api_token="tok", account_id="acct")
        cfg = await cf_pages.get_config(db, "local")
        assert cfg == {"api_token": "tok", "account_id": "acct"}
        assert await cf_pages.clear_config(db, "local") is True
        assert await cf_pages.get_config(db, "local") is None

    async def test_requires_all_fields(self, db):
        with pytest.raises(cf_pages.CloudflareError):
            await cf_pages.set_config(db, "local", api_token="", account_id="a")


class TestProjectNaming:
    def test_normalize_rejects_bad(self):
        with pytest.raises(cf_pages.CloudflareError):
            cf_pages.normalize_project("Bad Name!")

    def test_normalize_lowercases(self):
        assert cf_pages.normalize_project("My-Site") == "my-site"

    def test_slug_from_title(self):
        assert cf_pages.slug_project("Catering Landing Page!") == "catering-landing-page"

    def test_slug_never_empty(self):
        assert cf_pages.slug_project("###") == "batonkeep-site"


# ── Deploy orchestration (wrangler mocked) ────────────────────────────────────

def _workspace_with_site(tmp_path):
    root = str(tmp_path / "ws")
    os.makedirs(root)
    with open(os.path.join(root, "index.html"), "w") as f:
        f.write("<h1>hi</h1>")
    return root


class TestDeploy:
    async def test_deploy_happy_path(self, tmp_path, monkeypatch):
        root = _workspace_with_site(tmp_path)
        calls = []

        async def fake_run(cmd, env):
            calls.append(cmd)
            if cmd[2] == "project":  # project create
                return 1, "project already exists"
            return 0, "Deployment complete! https://h.my-site.pages.dev"

        monkeypatch.setattr(cf_pages.shutil, "which", lambda _: "/usr/bin/wrangler")
        monkeypatch.setattr(cf_pages, "_run", fake_run)

        res = await cf_pages.deploy(root, {"api_token": "t", "account_id": "a"}, "my-site")
        assert res == {"url": "https://h.my-site.pages.dev", "project": "my-site"}
        # Both project-create and deploy were attempted, in order.
        assert calls[0][2] == "project" and calls[1][2] == "deploy"

    async def test_deploy_requires_wrangler(self, tmp_path, monkeypatch):
        root = _workspace_with_site(tmp_path)
        monkeypatch.setattr(cf_pages.shutil, "which", lambda _: None)
        with pytest.raises(cf_pages.CloudflareError, match="wrangler"):
            await cf_pages.deploy(root, {"api_token": "t", "account_id": "a"}, "p")

    async def test_deploy_surfaces_failure(self, tmp_path, monkeypatch):
        root = _workspace_with_site(tmp_path)

        async def fake_run(cmd, env):
            return (0, "ok") if cmd[2] == "project" else (1, "Authentication error [code: 10000]")

        monkeypatch.setattr(cf_pages.shutil, "which", lambda _: "/usr/bin/wrangler")
        monkeypatch.setattr(cf_pages, "_run", fake_run)
        with pytest.raises(cf_pages.CloudflareError, match="Authentication error"):
            await cf_pages.deploy(root, {"api_token": "t", "account_id": "a"}, "p")

    async def test_deploy_empty_build(self, tmp_path, monkeypatch):
        root = str(tmp_path / "empty")
        os.makedirs(root)
        monkeypatch.setattr(cf_pages.shutil, "which", lambda _: "/usr/bin/wrangler")
        with pytest.raises(cf_pages.CloudflareError, match="nothing to publish"):
            await cf_pages.deploy(root, {"api_token": "t", "account_id": "a"}, "p")


class TestBundle:
    def test_materialize_publishes_build_output_not_source(self, tmp_path):
        # A Vite-style workspace: a *source* index.html at the root (points at
        # /src/main.tsx, which a browser can't run) and the real built site in
        # dist/. The Cloudflare bundle must ship dist/, not the source tree —
        # otherwise the deployed Pages site is blank.
        root = str(tmp_path / "ws")
        os.makedirs(os.path.join(root, "src"))
        os.makedirs(os.path.join(root, "dist", "assets"))
        with open(os.path.join(root, "index.html"), "w") as f:
            f.write('<script type="module" src="/src/main.tsx"></script>')
        with open(os.path.join(root, "src", "main.tsx"), "w") as f:
            f.write("createRoot(...)")
        with open(os.path.join(root, "dist", "index.html"), "w") as f:
            f.write('<script type="module" src="./assets/index-abc.js"></script>')
        with open(os.path.join(root, "dist", "assets", "index-abc.js"), "w") as f:
            f.write("console.log('built')")

        out = cf_pages._materialize_bundle(root)
        try:
            files = {
                os.path.relpath(os.path.join(dp, f), out)
                for dp, _, fs in os.walk(out) for f in fs
            }
            # Built site shipped at the bundle root; source entry point absent.
            assert "index.html" in files
            assert os.path.join("assets", "index-abc.js") in files
            assert os.path.join("src", "main.tsx") not in files
            with open(os.path.join(out, "index.html")) as f:
                assert "/src/main.tsx" not in f.read()
        finally:
            import shutil
            shutil.rmtree(out, ignore_errors=True)


# ── Routes ────────────────────────────────────────────────────────────────────

class TestRoutes:
    def test_config_and_deploy_routes(self, tmp_path, monkeypatch):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id
        from app.sessions import workspace as ws, cf_pages as cf

        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            root = await ws.create_workspace("s1", title="My Build", goal="G")
            with open(os.path.join(root, "index.html"), "w") as f:
                f.write("<h1>hi</h1>")
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add_all([
                    Owner(id="local", label="Me"),
                    SessionModel(id="s1", owner_id="local", title="My Build", provider="mock",
                                 workspace_path=root, preview_token="t", status="active"),
                ])
                await db.commit()
            return Maker

        Maker = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override_db():
            async with Maker() as db:
                yield db

        # Deploy is mocked — we verify wiring, the project resolution, and the
        # "not configured" gate, not wrangler. Capture the project it was called with.
        seen = {}

        async def fake_deploy(workspace, config, project, **kw):
            assert config["api_token"] == "tok"  # token reaches the backend connector
            seen["project"] = project
            return {"url": f"https://h.{project}.pages.dev", "project": project}

        monkeypatch.setattr(cf, "deploy", fake_deploy)
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)

            # Deploy before config → 400.
            assert c.post("/api/sessions/s1/publish/cloudflare").status_code == 400
            assert c.get("/api/integrations/cloudflare").json()["configured"] is False

            # Configure owner-level credentials (token + account only — no project).
            r = c.put("/api/integrations/cloudflare", json={
                "api_token": "tok", "account_id": "acct",
            })
            assert r.status_code == 200 and r.json()["configured"] is True

            # Status never leaks the token, and has no project (it's per-session).
            st = c.get("/api/integrations/cloudflare").json()
            assert st == {"configured": True, "account_id": "acct"}
            assert "tok" not in c.get("/api/integrations/cloudflare").text

            # Deploy with no project → defaults from the session title slug.
            d = c.post("/api/sessions/s1/publish/cloudflare")
            assert d.status_code == 200
            assert seen["project"] == "my-build"  # slug of title "My Build"
            assert d.json()["project"] == "my-build"

            # The session remembered the project; an explicit override wins next time.
            assert c.get("/api/sessions/s1").json()["cf_project"] == "my-build"
            d2 = c.post("/api/sessions/s1/publish/cloudflare", json={"project_name": "other-site"})
            assert seen["project"] == "other-site"
            assert c.get("/api/sessions/s1").json()["cf_project"] == "other-site"

            # Delete.
            assert c.delete("/api/integrations/cloudflare").status_code == 204
            assert c.get("/api/integrations/cloudflare").json()["configured"] is False
        finally:
            app.dependency_overrides.clear()
