"""
tests/test_imports.py — import an existing site (zip/tar) into a session workspace.

Verifies structure is preserved, a single wrapper dir is stripped, archive bombs /
traversal / non-regular entries are rejected or skipped, and .git/SESSION.md are
dropped. Plus the route extracts + commits a version.
"""
from __future__ import annotations

import io
import os
import tarfile
import zipfile

import pytest

from app.sessions import imports


def _zip(tmp_path, files: dict[str, bytes], name="site.zip") -> str:
    p = str(tmp_path / name)
    with zipfile.ZipFile(p, "w") as zf:
        for rel, data in files.items():
            zf.writestr(rel, data)
    return p


def _tar(tmp_path, files: dict[str, bytes], name="site.tar.gz") -> str:
    p = str(tmp_path / name)
    with tarfile.open(p, "w:gz") as tf:
        for rel, data in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return p


def _ws(tmp_path) -> str:
    root = str(tmp_path / "ws")
    os.makedirs(root)
    return root


class TestExtract:
    def test_zip_preserves_structure(self, tmp_path):
        arc = _zip(tmp_path, {
            "index.html": b"<h1>hi</h1>",
            "css/style.css": b"body{}",
            "js/app.js": b"console.log(1)",
        })
        ws = _ws(tmp_path)
        paths = imports.extract_archive(ws, arc)
        assert paths == ["css/style.css", "index.html", "js/app.js"]
        assert os.path.isfile(os.path.join(ws, "css", "style.css"))

    def test_strips_single_wrapper_dir(self, tmp_path):
        arc = _zip(tmp_path, {
            "mysite/index.html": b"x",
            "mysite/css/style.css": b"y",
        })
        ws = _ws(tmp_path)
        paths = imports.extract_archive(ws, arc)
        assert paths == ["css/style.css", "index.html"]  # "mysite/" stripped

    def test_keeps_root_files_no_strip(self, tmp_path):
        arc = _zip(tmp_path, {"index.html": b"x", "about/index.html": b"y"})
        ws = _ws(tmp_path)
        paths = imports.extract_archive(ws, arc)
        assert "index.html" in paths and "about/index.html" in paths

    def test_drops_git_and_brief_and_macosx(self, tmp_path):
        arc = _zip(tmp_path, {
            "index.html": b"x",
            ".git/config": b"[core]",
            "SESSION.md": b"brief",
            "__MACOSX/foo": b"junk",
        })
        ws = _ws(tmp_path)
        paths = imports.extract_archive(ws, arc)
        assert paths == ["index.html"]

    def test_rejects_traversal(self, tmp_path):
        arc = _zip(tmp_path, {"../evil.txt": b"x", "ok.html": b"y"})
        ws = _ws(tmp_path)
        paths = imports.extract_archive(ws, arc)
        assert paths == ["ok.html"]
        assert not os.path.exists(os.path.join(os.path.dirname(ws), "evil.txt"))

    def test_tar_gz_works(self, tmp_path):
        arc = _tar(tmp_path, {"site/index.html": b"x", "site/data.json": b"{}"})
        ws = _ws(tmp_path)
        paths = imports.extract_archive(ws, arc)
        assert paths == ["data.json", "index.html"]

    def test_unsupported_archive(self, tmp_path):
        p = str(tmp_path / "notarchive.txt")
        with open(p, "wb") as f:
            f.write(b"just text")
        with pytest.raises(imports.ImportArchiveError) as ei:
            imports.extract_archive(_ws(tmp_path), p)
        assert ei.value.status == 415

    def test_empty_archive(self, tmp_path):
        arc = _zip(tmp_path, {})
        with pytest.raises(imports.ImportArchiveError) as ei:
            imports.extract_archive(_ws(tmp_path), arc)
        assert ei.value.status == 400


class TestGitClone:
    async def test_rejects_non_https(self, tmp_path):
        with pytest.raises(imports.ImportArchiveError) as ei:
            await imports.clone_repo(_ws(tmp_path), "git@github.com:owner/repo.git")
        assert ei.value.status == 400

    async def test_rejects_internal_host(self, tmp_path, monkeypatch):
        import socket
        # Resolve the host to a private IP → SSRF guard must refuse.
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, "", ("127.0.0.1", 443))],
        )
        with pytest.raises(imports.ImportArchiveError) as ei:
            await imports.clone_repo(_ws(tmp_path), "https://internal.local/repo.git")
        assert ei.value.status == 400

    async def test_happy_path_imports_working_tree(self, tmp_path, monkeypatch):
        ws = _ws(tmp_path)

        async def fake_run(cmd, env, cwd=None):
            # Simulate a clone: populate the target dir (last arg) like git would.
            target = cmd[-1]
            os.makedirs(os.path.join(target, ".git"))
            with open(os.path.join(target, ".git", "config"), "w") as f:
                f.write("[core]")
            os.makedirs(os.path.join(target, "src"))
            with open(os.path.join(target, "index.html"), "w") as f:
                f.write("<h1>cloned</h1>")
            with open(os.path.join(target, "src", "app.js"), "w") as f:
                f.write("x")
            # Credential prompts disabled so private repos fail fast.
            assert env.get("GIT_TERMINAL_PROMPT") == "0"
            return 0, ""

        monkeypatch.setattr(imports.shutil, "which", lambda _: "/usr/bin/git")
        monkeypatch.setattr(imports, "_run_git", fake_run)
        paths = await imports.clone_repo(ws, "https://github.com/owner/repo.git")
        assert paths == ["index.html", "src/app.js"]  # .git dropped
        assert os.path.isfile(os.path.join(ws, "src", "app.js"))

    async def test_clone_failure_surfaced(self, tmp_path, monkeypatch):
        async def fake_run(cmd, env, cwd=None):
            return 128, "fatal: Authentication failed"

        monkeypatch.setattr(imports.shutil, "which", lambda _: "/usr/bin/git")
        monkeypatch.setattr(imports, "_run_git", fake_run)
        with pytest.raises(imports.ImportArchiveError) as ei:
            await imports.clone_repo(_ws(tmp_path), "https://github.com/owner/private.git")
        assert ei.value.status == 502


class TestRoute:
    def test_import_route_extracts_and_commits(self, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id
        from app.sessions import workspace as ws

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/imp.db")

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            root = await ws.create_workspace("s1", title="S", goal="G")
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add_all([
                    Owner(id="local", label="Me"),
                    SessionModel(id="s1", owner_id="local", title="S", provider="mock",
                                 workspace_path=root, preview_token="t", status="active"),
                ])
                await db.commit()
            return Maker, root

        Maker, root = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            arc = _zip(tmp_path, {"site/index.html": b"<h1>imported</h1>", "site/css/x.css": b"a{}"})
            c = TestClient(app)
            with open(arc, "rb") as f:
                r = c.post("/api/sessions/s1/import", files={"file": ("site.zip", f, "application/zip")})
            assert r.status_code == 201
            body = r.json()
            assert body["count"] == 2 and "index.html" in body["paths"]
            assert body["commit_sha"]
            # Files actually landed in the workspace, structure preserved.
            assert os.path.isfile(os.path.join(root, "css", "x.css"))
            # And are visible via the file browser.
            listing = c.get("/api/sessions/s1/files").json()
            assert "index.html" in [e["path"] for e in listing]
        finally:
            app.dependency_overrides.clear()
