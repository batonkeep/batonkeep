"""
tests/test_file_browser.py — session file browser / raw-file route (P-0016 b).

Verifies non-web artifacts are inspectable: the workspace listing excludes
internals, the raw route serves a file verbatim (no index.html fallback) and is
path-traversal / cross-workspace safe, and agent file:// links get rewritten to
the raw route.
"""
from __future__ import annotations

import os

import pytest

from app.sessions import workspace as ws
from app.sessions.preview import (
    PreviewError,
    resolve_workspace_file,
    rewrite_workspace_file_links,
)


def _make_ws(tmp_path):
    root = str(tmp_path / "ws")
    os.makedirs(os.path.join(root, "cache"))
    os.makedirs(os.path.join(root, ".git"))
    with open(os.path.join(root, "download_data.py"), "w") as f:
        f.write("print('hi')\n")
    with open(os.path.join(root, "cache", "projects.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(root, ws.BRIEF_FILENAME), "w") as f:
        f.write("# brief")
    with open(os.path.join(root, ".git", "config"), "w") as f:
        f.write("[core]")
    return root


class TestListing:
    def test_excludes_git_and_brief(self, tmp_path):
        root = _make_ws(tmp_path)
        paths = [e["path"] for e in ws.list_files_meta(root)]
        assert "download_data.py" in paths
        assert os.path.join("cache", "projects.json") in paths
        assert ws.BRIEF_FILENAME not in paths
        assert not any(p.startswith(".git") for p in paths)

    def test_entries_have_size_and_mtime(self, tmp_path):
        root = _make_ws(tmp_path)
        entry = next(e for e in ws.list_files_meta(root) if e["path"] == "download_data.py")
        assert entry["size"] > 0
        assert entry["modified"] > 0


class TestRawResolve:
    def test_serves_exact_file_no_index_fallback(self, tmp_path):
        root = _make_ws(tmp_path)
        path, media = resolve_workspace_file(root, "download_data.py")
        assert path.endswith("download_data.py")
        # text/x-python on most platforms; just assert a type was guessed.
        assert media

    def test_directory_is_404_not_index(self, tmp_path):
        root = _make_ws(tmp_path)
        # No index.html fallback: requesting a dir 404s instead of serving index.
        with pytest.raises(PreviewError):
            resolve_workspace_file(root, "cache")

    def test_empty_path_404(self, tmp_path):
        root = _make_ws(tmp_path)
        with pytest.raises(PreviewError):
            resolve_workspace_file(root, "")

    def test_traversal_rejected(self, tmp_path):
        root = _make_ws(tmp_path)
        with pytest.raises(PreviewError):
            resolve_workspace_file(root, "../../etc/passwd")

    def test_missing_file_404(self, tmp_path):
        root = _make_ws(tmp_path)
        with pytest.raises(PreviewError):
            resolve_workspace_file(root, "nope.txt")


class TestLinkRewrite:
    def test_rewrites_own_workspace_link(self, tmp_path):
        root = os.path.abspath(str(tmp_path / "ws"))
        sid = "abc123"
        text = f"see [download_data.py](file://{root}/download_data.py) for details"
        out = rewrite_workspace_file_links(text, sid, root)
        assert f"/api/sessions/{sid}/files/raw/download_data.py" in out
        assert "file://" not in out

    def test_triple_slash_form(self, tmp_path):
        # workspace path starts with "/", so file://<root> reads as file:///…
        root = os.path.abspath(str(tmp_path / "ws"))
        sid = "s1"
        text = f"[x](file://{root}/cache/projects.json)"
        out = rewrite_workspace_file_links(text, sid, root)
        assert f"/api/sessions/{sid}/files/raw/cache/projects.json" in out

    def test_leaves_other_file_links_untouched(self, tmp_path):
        root = os.path.abspath(str(tmp_path / "ws"))
        other = "/some/other/path/secret.txt"
        text = f"[a](file://{root}/ok.py) and [b](file://{other})"
        out = rewrite_workspace_file_links(text, "s", root)
        assert f"file://{other}" in out  # untouched
        assert "/files/raw/ok.py" in out


class TestFileBrowserHTTP:
    """list + raw-file endpoints, with owner_id isolation (P-0016 b gate)."""

    def test_list_raw_traversal_and_owner_isolation(self, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fb.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            root_a = await ws.create_workspace("sa", title="A", goal="G")
            with open(os.path.join(root_a, "download_data.py"), "w") as f:
                f.write("print('hi')\n")
            root_b = await ws.create_workspace("sb", title="B", goal="G")
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add_all([
                    Owner(id="local", label="Me"),
                    Owner(id="other", label="Them"),
                    SessionModel(id="sa", owner_id="local", title="A", provider="mock",
                                 workspace_path=root_a, preview_token="t", status="active"),
                    SessionModel(id="sb", owner_id="other", title="B", provider="mock",
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

            # List excludes SESSION.md; includes the generated script.
            listing = c.get("/api/sessions/sa/files")
            assert listing.status_code == 200
            paths = [e["path"] for e in listing.json()]
            assert "download_data.py" in paths
            assert "SESSION.md" not in paths

            # Raw file served verbatim (no index fallback).
            raw = c.get("/api/sessions/sa/files/raw/download_data.py")
            assert raw.status_code == 200
            assert raw.text == "print('hi')\n"

            # Download flag sets attachment disposition.
            dl = c.get("/api/sessions/sa/files/raw/download_data.py?download=1")
            assert "attachment" in dl.headers.get("content-disposition", "")

            # Missing file + traversal both 404.
            assert c.get("/api/sessions/sa/files/raw/nope.txt").status_code == 404
            assert c.get("/api/sessions/sa/files/raw/../../etc/passwd").status_code == 404

            # Cross-owner: caller "local" cannot list/read "other"'s session.
            assert c.get("/api/sessions/sb/files").status_code == 404
            assert c.get("/api/sessions/sb/files/raw/anything").status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_turns_endpoint_rewrites_legacy_file_links(self, tmp_path):
        # A turn persisted BEFORE this feature still has a raw file:// link; the
        # turns endpoint rewrites it on read so the existing session renders fixed.
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel, SessionTurn
        from app.main import app, _owner_id

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            root = await ws.create_workspace("sx", title="X", goal="G")
            legacy = f"see [download_data.py](file://{root}/download_data.py)"
            Maker = async_sessionmaker(engine, expire_on_commit=False)
            async with Maker() as db:
                db.add_all([
                    Owner(id="local", label="Me"),
                    SessionModel(id="sx", owner_id="local", title="X", provider="mock",
                                 workspace_path=root, preview_token="t", status="active"),
                    SessionTurn(session_id="sx", owner_id="local", seq=0, provider="mock",
                                prompt="go", response=legacy, status="succeeded"),
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
            turns = c.get("/api/sessions/sx/turns")
            assert turns.status_code == 200
            resp = turns.json()[0]["response"]
            assert "/api/sessions/sx/files/raw/download_data.py" in resp
            assert "file://" not in resp
        finally:
            app.dependency_overrides.clear()
