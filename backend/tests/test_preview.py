"""
tests/test_preview.py — M1.2 gate: live preview serving + session auth.

Verify gate (PLAN §M1.2):
  a built static page renders in the in-UI preview pane; the preview is not
  reachable without session auth.
"""
from __future__ import annotations

import os

import pytest

from app.sessions.preview import (
    PreviewError,
    check_token,
    guess_media_type,
    guess_preview_media_type,
    resolve_preview_file,
    rewrite_html_root_paths,
)


class TestGuessMediaType:
    """D-0028: text/code/markdown render inline; images keep image/*; unknown → octet."""

    def test_markdown_served_as_utf8_text(self):
        assert guess_media_type("README.md") == "text/plain; charset=utf-8"

    def test_code_served_as_utf8_text(self):
        for name in ("app.py", "main.ts", "style.css", "data.json"):
            assert guess_media_type(name) == "text/plain; charset=utf-8", name

    def test_dotfile_gitignore_served_as_text(self):
        assert guess_media_type(".gitignore") == "text/plain; charset=utf-8"

    def test_image_keeps_image_mime(self):
        assert guess_media_type("logo.png") == "image/png"

    def test_unknown_binary_is_octet_stream(self):
        assert guess_media_type("blob.bin") == "application/octet-stream"


class TestGuessPreviewMediaType:
    """Rendered preview keeps real CSS/JS types — text/plain stylesheets/scripts
    are rejected by browsers, silently unstyling the previewed site."""

    def test_css_and_js_keep_real_types(self):
        assert guess_preview_media_type("style.css") == "text/css; charset=utf-8"
        assert guess_preview_media_type("app.js") == "text/javascript; charset=utf-8"
        assert guess_preview_media_type("mod.mjs") == "text/javascript; charset=utf-8"
        assert guess_preview_media_type("data.json") == "application/json; charset=utf-8"

    def test_other_text_still_plain(self):
        assert guess_preview_media_type("notes.md") == "text/plain; charset=utf-8"
        assert guess_preview_media_type("script.py") == "text/plain; charset=utf-8"


class TestRewriteHtmlRootPaths:
    BASE = "/api/sessions/s1/preview/tok"

    def test_root_absolute_src_href_prefixed(self):
        html = '<script src="/assets/index-abc.js"></script><link href="/style.css">'
        out = rewrite_html_root_paths(html, self.BASE)
        assert f'src="{self.BASE}/assets/index-abc.js"' in out
        assert f'href="{self.BASE}/style.css"' in out

    def test_relative_and_external_urls_untouched(self):
        html = '<img src="logo.png"><a href="https://x.io"><script src="//cdn.x/y.js">'
        assert rewrite_html_root_paths(html, self.BASE) == html


class TestPreviewResolution:
    def _make_site(self, tmp_path):
        root = str(tmp_path / "ws")
        os.makedirs(os.path.join(root, "assets"))
        with open(os.path.join(root, "index.html"), "w") as f:
            f.write("<h1>hello</h1>")
        with open(os.path.join(root, "assets", "logo.svg"), "w") as f:
            f.write("<svg/>")
        return root

    def test_root_serves_index(self, tmp_path):
        root = self._make_site(tmp_path)
        path, media = resolve_preview_file(root, "")
        assert path.endswith("index.html")
        assert media == "text/html"

    def test_nested_asset_served_with_mime(self, tmp_path):
        root = self._make_site(tmp_path)
        path, media = resolve_preview_file(root, "assets/logo.svg")
        assert path.endswith("logo.svg")
        assert media in ("image/svg+xml", "application/octet-stream")

    def test_missing_file_404(self, tmp_path):
        root = self._make_site(tmp_path)
        with pytest.raises(PreviewError) as ei:
            resolve_preview_file(root, "nope.html")
        assert ei.value.status == 404

    def test_path_traversal_blocked(self, tmp_path):
        root = self._make_site(tmp_path)
        # would escape to a sibling secret file
        with open(os.path.join(str(tmp_path), "secret.txt"), "w") as f:
            f.write("topsecret")
        with pytest.raises(PreviewError) as ei:
            resolve_preview_file(root, "../secret.txt")
        assert ei.value.status == 404

    def test_build_dir_preferred_over_root_source_index(self, tmp_path):
        # A bundled project: root index.html is the source template (unrunnable
        # in a browser); the built site in dist/ is what the preview must serve.
        root = self._make_site(tmp_path)
        os.makedirs(os.path.join(root, "dist", "assets"))
        with open(os.path.join(root, "dist", "index.html"), "w") as f:
            f.write("<h1>built</h1>")
        with open(os.path.join(root, "dist", "assets", "index-abc.js"), "w") as f:
            f.write("console.log(1)")

        path, media = resolve_preview_file(root, "")
        assert path.endswith(os.path.join("dist", "index.html"))
        assert media == "text/html"
        # Asset requests resolve under dist/ too.
        path, media = resolve_preview_file(root, "assets/index-abc.js")
        assert path.endswith(os.path.join("dist", "assets", "index-abc.js"))
        assert media == "text/javascript; charset=utf-8"
        # Explicit workspace paths still reachable (file links, root assets).
        path, _ = resolve_preview_file(root, "dist/index.html")
        assert path.endswith(os.path.join("dist", "index.html"))
        path, _ = resolve_preview_file(root, "assets/logo.svg")
        assert path.endswith(os.path.join("ws", "assets", "logo.svg"))

    def test_directory_without_index_404(self, tmp_path):
        root = str(tmp_path / "empty")
        os.makedirs(root)
        with pytest.raises(PreviewError) as ei:
            resolve_preview_file(root, "")
        assert ei.value.status == 404


class TestPreviewAuth:
    def test_correct_token_passes(self):
        check_token("secret", "secret")  # no raise

    def test_missing_token_403(self):
        with pytest.raises(PreviewError) as ei:
            check_token("secret", None)
        assert ei.value.status == 403

    def test_wrong_token_403(self):
        with pytest.raises(PreviewError) as ei:
            check_token("secret", "guess")
        assert ei.value.status == 403

    def test_empty_expected_never_authorizes(self):
        # A session with no token set must never be previewable.
        with pytest.raises(PreviewError):
            check_token("", "")


# ── End-to-end through the app (renders + auth) ───────────────────────────────

class TestPreviewEndpoint:
    """
    Exercises the real route with dependency-overridden DB (the module engine
    points at a container path), proving: renders a built page WITH the token,
    refuses WITHOUT it.
    """

    def test_preview_renders_built_page_with_token_and_blocks_without(self, tmp_path):
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id

        # Fresh in-process DB.
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", echo=False)
        import asyncio

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.get_event_loop().run_until_complete(_setup())
        Maker = async_sessionmaker(engine, expire_on_commit=False)

        # Build a session workspace with an agent-built page.
        root = str(tmp_path / "ws")
        os.makedirs(root)
        with open(os.path.join(root, "index.html"), "w") as f:
            f.write("<h1>built by the agent</h1>")

        async def _seed():
            async with Maker() as db:
                db.add(Owner(id="local", label="Test"))
                db.add(SessionModel(
                    id="s1", owner_id="local", title="Landing", provider="mock",
                    workspace_path=root, preview_token="tok-123", status="active",
                ))
                await db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)  # no lifespan context → module engine untouched

            # Token is a path segment so relative assets resolve under the same base.
            ok = c.get("/api/sessions/s1/preview/tok-123")
            assert ok.status_code == 200
            assert "built by the agent" in ok.text
            assert ok.headers.get("cache-control") == "no-store"

            # A relative sub-asset request (as a browser would issue it) is authed too.
            with open(os.path.join(root, "style.css"), "w") as f:
                f.write("body{color:red}")
            asset = c.get("/api/sessions/s1/preview/tok-123/style.css")
            assert asset.status_code == 200
            assert "color:red" in asset.text

            assert c.get("/api/sessions/s1/preview/wrong").status_code == 403       # wrong token
            assert c.get("/api/sessions/s1/preview/wrong/style.css").status_code == 403
            assert c.get("/api/sessions/nope/preview/tok-123").status_code == 404
        finally:
            app.dependency_overrides.clear()
            asyncio.get_event_loop().run_until_complete(engine.dispose())
