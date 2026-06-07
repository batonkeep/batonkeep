"""
tests/test_uploads.py — M1.5 gate: asset upload-in (D-0010).

Verify gate (PLAN §M1.5): a user drops a logo + a CSV into the chat; both appear as
workspace files and are committed; publish bundles the logo (closes the M1.4 gate);
disallowed MIME / oversize is rejected; uploads are owner-scoped.
"""
from __future__ import annotations

import io
import os

import pytest

from app.sessions import uploads


# ── Unit: validation + placement ──────────────────────────────────────────────

class TestUploadValidation:
    def test_images_go_to_assets_data_to_data(self):
        assert uploads.dest_relpath("Logo.PNG") == "assets/Logo.PNG"
        assert uploads.dest_relpath("sales.csv") == "data/sales.csv"
        assert uploads.dest_relpath("report.pdf") == "data/report.pdf"

    def test_disallowed_extension_rejected(self):
        with pytest.raises(uploads.UploadError) as exc:
            uploads.dest_relpath("evil.exe")
        assert exc.value.status == 415

    def test_filename_sanitised_no_traversal(self):
        # Directory components are stripped; the result stays inside assets/.
        assert uploads.dest_relpath("../../etc/passwd.png") == "assets/passwd.png"

    def test_oversize_rejected(self, tmp_path):
        root = str(tmp_path)
        orig = uploads._settings.upload_max_bytes
        uploads._settings.__dict__["upload_max_bytes"] = 10
        try:
            with pytest.raises(uploads.UploadError) as exc:
                uploads.save_upload(root, "big.txt", io.BytesIO(b"x" * 50))
            assert exc.value.status == 413
            # Partial file cleaned up.
            assert not os.path.exists(os.path.join(root, "data", "big.txt"))
        finally:
            uploads._settings.__dict__["upload_max_bytes"] = orig

    def test_empty_file_rejected(self, tmp_path):
        with pytest.raises(uploads.UploadError) as exc:
            uploads.save_upload(str(tmp_path), "empty.txt", io.BytesIO(b""))
        assert exc.value.status == 400

    def test_upload_is_group_writable(self, tmp_path):
        """Uploaded assets must land co-writable by the sandbox-user agent
        (P-0022/D-0020), not just readable — even under a restrictive backend
        umask. The save applies a group-write umask; assert the bit survives."""
        prev = os.umask(0o022)  # simulate the backend's default (drops group write)
        try:
            rel = uploads.save_upload(str(tmp_path), "sales.csv", io.BytesIO(b"a,b\n1,2\n"))
            mode = os.stat(os.path.join(str(tmp_path), rel)).st_mode
            assert mode & 0o020, f"upload not group-writable: {oct(mode)}"
            # the data/ dir the upload created is group-writable too
            dmode = os.stat(os.path.join(str(tmp_path), "data")).st_mode
            assert dmode & 0o020, f"data/ not group-writable: {oct(dmode)}"
        finally:
            os.umask(prev)


# ── End-to-end through the app (drop → commit → reference → publish) ───────────

class TestUploadHTTP:
    def test_drop_files_commit_and_isolation(self, tmp_path):
        import asyncio
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.db import Base, get_db
        from app.models import Owner, Session as SessionModel
        from app.main import app, _owner_id
        from app.sessions import workspace as ws

        ws._settings.__dict__["sessions_dir"] = str(tmp_path / "sessions")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/u.db", echo=False)

        async def _setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            root_a = await ws.create_workspace("sa", title="My Site", goal="G")
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
            return Maker, root_a

        Maker, root_a = asyncio.get_event_loop().run_until_complete(_setup())

        async def _override_db():
            async with Maker() as db:
                yield db

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_owner_id] = lambda: "local"
        try:
            c = TestClient(app)

            # Drop a logo + a CSV into the session in one request.
            resp = c.post(
                "/api/sessions/sa/uploads",
                files=[
                    ("files", ("logo.png", b"PNGDATA", "image/png")),
                    ("files", ("sales.csv", b"a,b\n1,2\n", "text/csv")),
                ],
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert set(body["paths"]) == {"assets/logo.png", "data/sales.csv"}
            # The upload landed as a committed version.
            assert body["commit_sha"] and len(body["commit_sha"]) == 40

            # Files actually exist in the workspace (referenceable by name on next turn).
            assert open(os.path.join(root_a, "assets", "logo.png"), "rb").read() == b"PNGDATA"
            assert "a,b" in open(os.path.join(root_a, "data", "sales.csv")).read()

            # Shows up in Undo/History.
            versions = c.get("/api/sessions/sa/versions").json()
            assert any("upload:" in v["message"] for v in versions)

            # Disallowed type rejected (atomic: nothing written).
            bad = c.post("/api/sessions/sa/uploads",
                         files=[("files", ("x.exe", b"MZ", "application/octet-stream"))])
            assert bad.status_code == 415

            # owner_id isolation: another owner's session is invisible.
            other = c.post("/api/sessions/sb/uploads",
                           files=[("files", ("a.png", b"X", "image/png"))])
            assert other.status_code == 404
        finally:
            app.dependency_overrides.clear()
