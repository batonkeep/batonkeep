"""
tests/test_publish.py — M1.4 gate: publish + share.

Verify gate (PLAN §M1.4): a published artifact is reachable at its share URL with
its bundled assets (e.g. an uploaded logo renders); revocation 404s it; nothing
else in the workspace is exposed. Plus the download pack (#1) and owner scoping.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest


# ── publish.py unit tests ─────────────────────────────────────────────────────

class TestPublishBundle:
    def _make_workspace(self, tmp_path):
        from app.sessions import publish as pub
        monkeypatch_dir = tmp_path / "workspace"
        os.makedirs(monkeypatch_dir)
        # Static assets + an excluded brief + a fake .git dir.
        (monkeypatch_dir / "index.html").write_text("<img src='assets/logo.png'>")
        os.makedirs(monkeypatch_dir / "assets")
        (monkeypatch_dir / "assets" / "logo.png").write_bytes(b"PNGDATA")
        (monkeypatch_dir / "SESSION.md").write_text("internal brief")
        os.makedirs(monkeypatch_dir / ".git")
        (monkeypatch_dir / ".git" / "config").write_text("secret")
        return str(monkeypatch_dir)

    def test_publishable_files_excludes_git_and_brief(self, tmp_path):
        from app.sessions import publish as pub
        ws_dir = self._make_workspace(tmp_path)
        files = pub._publishable_files(ws_dir)
        assert "index.html" in files
        assert os.path.join("assets", "logo.png") in files
        assert "SESSION.md" not in files
        assert not any(f.startswith(".git") for f in files)

    def test_publishable_files_excludes_symlinks_escaping_workspace(self, tmp_path):
        # A sandbox agent could plant a symlink pointing at a control-plane file
        # (e.g. /data/batonkeep.db). The backend user can read it, so it must never
        # be followed into a publicly-served bundle.
        from app.sessions import publish as pub
        ws_dir = self._make_workspace(tmp_path)
        secret = tmp_path / "outside_secret.txt"
        secret.write_text("encrypted-keys-and-run-history")
        os.symlink(str(secret), os.path.join(ws_dir, "leak.txt"))

        files = pub._publishable_files(ws_dir)
        assert "leak.txt" not in files
        # Real assets still publish.
        assert "index.html" in files

    def test_build_bundle_materializes_assets(self, tmp_path, monkeypatch):
        from app.sessions import publish as pub
        monkeypatch.setattr(pub._settings, "publish_dir", str(tmp_path / "pub"), raising=False)
        ws_dir = self._make_workspace(tmp_path)

        dest = pub.build_bundle(ws_dir, "tok123")
        assert os.path.isfile(os.path.join(dest, "index.html"))
        assert os.path.isfile(os.path.join(dest, "assets", "logo.png"))
        assert not os.path.exists(os.path.join(dest, "SESSION.md"))
        assert not os.path.exists(os.path.join(dest, ".git"))

        # remove_bundle cleans it up.
        pub.remove_bundle("tok123", dest)
        assert not os.path.exists(dest)

    def test_build_bundle_publishes_built_site_from_dist(self, tmp_path, monkeypatch):
        # A bundled project (Vite/CRA): the site is dist/, not the source tree.
        # The bundle must serve dist's contents at its root — publishing the
        # workspace root would ship source and exclude dist (D-0029), leaving the
        # shared link blank while the preview (which prefers dist) works.
        from app.sessions import publish as pub
        monkeypatch.setattr(pub._settings, "publish_dir", str(tmp_path / "pub"), raising=False)
        ws_dir = self._make_workspace(tmp_path)  # root index.html = source template
        os.makedirs(os.path.join(ws_dir, "dist", "assets"))
        with open(os.path.join(ws_dir, "dist", "index.html"), "w") as f:
            f.write("<h1>built</h1>")
        with open(os.path.join(ws_dir, "dist", "assets", "index-abc.js"), "w") as f:
            f.write("console.log(1)")

        dest = pub.build_bundle(ws_dir, "tok-dist")
        with open(os.path.join(dest, "index.html")) as f:
            assert "built" in f.read()
        assert os.path.isfile(os.path.join(dest, "assets", "index-abc.js"))
        # Source tree (workspace-root assets) is not in the bundle.
        assert not os.path.exists(os.path.join(dest, "dist"))
        assert not os.path.exists(os.path.join(dest, "assets", "logo.png"))

        # The download pack ships the same built site.
        data = pub.zip_workspace(ws_dir)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert "index.html" in names
        assert "assets/index-abc.js" in names

    def test_publishable_files_excludes_package_dirs(self, tmp_path):
        # D-0029: package/build artifact dirs are pruned at any depth so they never
        # ride along in the download pack or share bundle.
        from app.sessions import publish as pub
        ws_dir = self._make_workspace(tmp_path)
        # A root-level node_modules and a nested __pycache__ + .venv.
        os.makedirs(os.path.join(ws_dir, "node_modules", "left-pad"))
        open(os.path.join(ws_dir, "node_modules", "left-pad", "index.js"), "w").close()
        os.makedirs(os.path.join(ws_dir, "src", "__pycache__"))
        open(os.path.join(ws_dir, "src", "__pycache__", "m.pyc"), "w").close()
        (tmp_path / "workspace" / "src").joinpath("app.py").write_text("print(1)")
        os.makedirs(os.path.join(ws_dir, "api", ".venv", "bin"))
        open(os.path.join(ws_dir, "api", ".venv", "bin", "python"), "w").close()

        files = pub._publishable_files(ws_dir)
        assert "index.html" in files
        assert os.path.join("src", "app.py") in files
        assert not any("node_modules" in f for f in files)
        assert not any("__pycache__" in f for f in files)
        assert not any(".venv" in f for f in files)

    def test_publish_token_dir_rejects_traversal(self, tmp_path, monkeypatch):
        from app.sessions import publish as pub
        monkeypatch.setattr(pub._settings, "publish_dir", str(tmp_path / "pub"), raising=False)
        with pytest.raises(ValueError):
            pub.publish_token_dir("../escape")

    def test_zip_workspace_contains_only_static_assets(self, tmp_path):
        from app.sessions import publish as pub
        ws_dir = self._make_workspace(tmp_path)
        data = pub.zip_workspace(ws_dir)
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        assert "index.html" in names
        assert "assets/logo.png" in names
        assert "SESSION.md" not in names
        assert not any(n.startswith(".git") for n in names)


# ── HTTP: publish / share / revoke / download + owner isolation ───────────────

class TestPublishHTTP:
    def test_publish_share_revoke_download_and_isolation(self, tmp_path, monkeypatch):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id
        from app.sessions import workspace as ws
        from app.sessions import publish as pub

        monkeypatch.setattr(ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False)
        monkeypatch.setattr(pub._settings, "publish_dir", str(tmp_path / "publish"), raising=False)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/p.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            # Owner "local" session with a built page + an uploaded asset; commit it.
            root_a = await ws.create_workspace("sa", title="My Site", goal="G")
            with open(os.path.join(root_a, "index.html"), "w") as f:
                f.write("<h1>hi</h1><img src='assets/logo.png'>")
            os.makedirs(os.path.join(root_a, "assets"))
            with open(os.path.join(root_a, "assets", "logo.png"), "wb") as f:
                f.write(b"PNGDATA")
            await ws.commit_turn(root_a, seq=0, provider="mock", summary="built")
            # Owner "other" session (for isolation).
            root_b = await ws.create_workspace("sb", title="Theirs", goal="G")
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add_all([
                    Owner(id="local", label="Me"),
                    Owner(id="other", label="Them"),
                    SessionModel(id="sa", owner_id="local", title="My Site", provider="mock",
                                 workspace_path=root_a, preview_token="t", status="active"),
                    SessionModel(id="sb", owner_id="other", title="Theirs", provider="mock",
                                 workspace_path=root_b, preview_token="t", status="active"),
                ])
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

            # Initially unpublished.
            assert c.get("/api/sessions/sa/publish").json()["published"] is False

            # Publish → share token + version present.
            pub_resp = c.post("/api/sessions/sa/publish")
            assert pub_resp.status_code == 200
            state = pub_resp.json()
            assert state["published"] is True
            token = state["share_token"]
            assert state["version"] and len(state["version"]) == 40
            assert state["share_path"] == f"/api/share/{token}/"

            # Public share route serves the page (root → index.html) + bundled asset.
            root = c.get(f"/api/share/{token}/")
            assert root.status_code == 200 and b"<h1>hi</h1>" in root.content
            logo = c.get(f"/api/share/{token}/assets/logo.png")
            assert logo.status_code == 200 and logo.content == b"PNGDATA"

            # Nothing else exposed: the internal brief + git internals 404 via share.
            assert c.get(f"/api/share/{token}/SESSION.md").status_code == 404
            assert c.get(f"/api/share/{token}/.git/config").status_code == 404

            # Download pack: zip of static assets, owner-scoped.
            dl = c.get("/api/sessions/sa/download")
            assert dl.status_code == 200
            assert dl.headers["content-type"] == "application/zip"
            names = set(zipfile.ZipFile(io.BytesIO(dl.content)).namelist())
            assert "index.html" in names and "assets/logo.png" in names
            assert "SESSION.md" not in names

            # Re-publish rotates the token: the old share link 404s.
            new_token = c.post("/api/sessions/sa/publish").json()["share_token"]
            assert new_token != token
            assert c.get(f"/api/share/{token}/").status_code == 404
            assert c.get(f"/api/share/{new_token}/").status_code == 200

            # Revoke → 404 + state cleared.
            assert c.delete("/api/sessions/sa/publish").json()["published"] is False
            assert c.get(f"/api/share/{new_token}/").status_code == 404
            assert c.get("/api/sessions/sa/publish").json()["published"] is False

            # owner_id isolation: another owner's session is invisible (404) on every route.
            assert c.get("/api/sessions/sb/publish").status_code == 404
            assert c.post("/api/sessions/sb/publish").status_code == 404
            assert c.delete("/api/sessions/sb/publish").status_code == 404
            assert c.get("/api/sessions/sb/download").status_code == 404
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())


class TestVcsInternalsExcluded:
    """P-0084 — a repository must never reach a package, share bundle or browser.

    Git objects retain deleted and superseded content absent from the working
    tree, so this is a disclosure fence, not bundle hygiene. Two holes existed:
    `_EXCLUDED_TOP` matched the exact name `.git` at the workspace root only, so
    (a) a displaced repo (`.git.old`/`.git_old`, whose alias the *agent* chooses)
    and (b) a nested repository at any depth — which needs no misbehaviour, a
    cloned dependency is enough — both packaged in full.
    """

    SENTINEL = "CONTENT-ONLY-IN-GIT-OBJECTS"

    def _tree(self, tmp_path):
        root = tmp_path / "ws"

        def w(rel: str, body: str = "x") -> None:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

        # Repositories under every shape we must catch.
        w(".git/HEAD", self.SENTINEL)
        w(".git/objects/ab/cd")
        w(".git/refs/heads/main")
        # Displaced-repo aliases — the agent picks the spelling, so name lists lose.
        w(".git.old/objects/ab/cd", self.SENTINEL)
        w(".git.old/config")
        w(".git_old/objects/cd/ef", self.SENTINEL)  # alias only: no HEAD/refs/config
        w(".git-old/HEAD", self.SENTINEL)
        w(".git-old/refs/heads/x")
        w(".git.bak/objects/z/z", self.SENTINEL)
        w(".git.bak/HEAD")
        # A nested repository needs no misbehaviour — a cloned dependency does it.
        w("subproject/.git/objects/12/34", self.SENTINEL)
        # Renamed with no VCS name at all: only shape detection catches this.
        w("vendored/oldrepo/objects/aa/bb", self.SENTINEL)
        w("vendored/oldrepo/HEAD")
        w("vendored/oldrepo/refs/heads/x")
        # A gitfile pointer is a FILE named `.git`, not a directory.
        w("wt/.git", "gitdir: /elsewhere")

        # Real artifacts that must keep publishing.
        w("index.html")
        w("subproject/main.py")
        w(".github/workflows/ci.yml")  # shares the `.git` prefix; must not match
        w(".gitignore")
        w(".gitattributes")
        w("assets/objects/model.obj")  # a bare `objects/` dir is plausible content
        return str(root)

    def _assert_clean(self, root, files):
        leaked = []
        for f in files:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                if self.SENTINEL in fh.read():
                    leaked.append(f)
        assert leaked == [], f"git internals in output: {leaked}"
        assert not any(
            part in {".git", ".git.old", ".git_old", ".git-old", ".git.bak"}
            for f in files for part in f.split(os.sep)
        ), f"VCS directory survived: {files}"
        for keep in (
            "index.html",
            os.path.join(".github", "workflows", "ci.yml"),
            ".gitignore",
            ".gitattributes",
            os.path.join("subproject", "main.py"),
            os.path.join("assets", "objects", "model.obj"),
        ):
            assert keep in files, f"over-pruned a real artifact: {keep}"

    def test_publish_and_package_walk_excludes_all_vcs_shapes(self, tmp_path):
        from app.sessions import publish as pub
        root = self._tree(tmp_path)
        self._assert_clean(root, pub._publishable_files(root))

    def test_file_browser_excludes_all_vcs_shapes(self, tmp_path):
        from app.sessions import workspace as ws
        root = self._tree(tmp_path)
        self._assert_clean(root, ws._listed_paths(root))

    def test_github_directory_is_not_treated_as_a_vcs_alias(self, tmp_path):
        """`.github/` shares the `.git` prefix and is a real artifact directory."""
        from app.sessions import workspace as ws
        d = tmp_path / ".github"
        d.mkdir()
        assert ws.is_vcs_internal(str(d)) is False

    def test_bare_objects_dir_without_markers_is_not_a_repository(self, tmp_path):
        """Shape needs a corroborating marker — over-pruning content is its own bug."""
        from app.sessions import workspace as ws
        d = tmp_path / "assets"
        (d / "objects").mkdir(parents=True)
        assert ws.is_vcs_internal(str(d)) is False
