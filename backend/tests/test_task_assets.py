"""
tests/test_task_assets.py — task-run generated-asset handling (P-0050/D-0046).

Covers the capture → promote → retention/storage pipeline that keeps a task run's
non-text artifacts (generated images, agent-written csv/pdf) instead of discarding
them with the scratch.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import task_assets, task_workspace
from app.db import Base
from app.models import Owner, Run, RunAsset, Task


@pytest.fixture
async def fresh_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add(Owner(id="local", label="Test"))
        await db.commit()
    yield Session, tmp_path
    await engine.dispose()


def _settings_to(monkeypatch, tmp_path):
    """Point work_dir + outputs_dir at the tmp tree (auto-restored by monkeypatch)."""
    monkeypatch.setattr(task_workspace._settings, "work_dir", str(tmp_path / "work"), raising=False)
    monkeypatch.setattr(task_workspace._settings, "outputs_dir", str(tmp_path / "outputs"), raising=False)
    # task_assets imports the same cached settings singleton, but set explicitly in
    # case that ever changes.
    monkeypatch.setattr(task_assets._settings, "outputs_dir", str(tmp_path / "outputs"), raising=False)


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_capture_scans_whole_scratch_with_allowlist(monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    workdir = task_workspace.prepare_current(1)
    # API-path convention (assets//data/) AND a CLI-lane file dropped at the cwd root
    # (agy saves there) — both must be captured.
    _write(os.path.join(workdir, "assets", "generated-1.png"), b"\x89PNG")
    _write(os.path.join(workdir, "data", "report.csv"), b"a,b\n1,2\n")
    _write(os.path.join(workdir, "daily-image.png"), b"\x89PNGroot")  # CLI lane: cwd root
    _write(os.path.join(workdir, "clip.mp4"), b"\x00\x00\x00 ftypmp4")  # media beyond upload allowlist
    # Skipped: source/scratch ext, the text report (md), and noise/dependency dirs.
    _write(os.path.join(workdir, "scratch.py"), b"print(1)")
    _write(os.path.join(workdir, "report.md"), b"# not an asset")
    _write(os.path.join(workdir, "node_modules", "lib", "icon.png"), b"libpng")
    _write(os.path.join(workdir, ".cache", "thumb.png"), b"cachepng")

    outputs = os.path.join(str(tmp_path / "outputs"), "run_5")
    os.makedirs(outputs, exist_ok=True)
    captured = task_workspace.capture_assets(workdir, outputs)

    rels = sorted(c["rel_path"] for c in captured)
    assert rels == ["assets/generated-1.png", "clip.mp4", "daily-image.png", "data/report.csv"]
    assert os.path.exists(os.path.join(outputs, "daily-image.png"))
    by_path = {c["rel_path"]: c for c in captured}
    assert by_path["daily-image.png"]["mime"] == "image/png"
    assert by_path["data/report.csv"]["bytes"] == len(b"a,b\n1,2\n")


def test_promote_copies_assets_readonly(monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    task_workspace.prepare_current(2)
    outputs = os.path.join(str(tmp_path / "outputs"), "run_9")
    _write(os.path.join(outputs, "output.md"), b"# report")
    _write(os.path.join(outputs, "assets", "img.png"), b"\x89PNG")
    _write(os.path.join(outputs, "daily-image.png"), b"\x89PNGroot")  # root-level asset

    task_workspace.promote(2, 9, outputs)
    hist = os.path.join(task_workspace._task_root(2), "history", "run_9")
    for rel in ("output.md", "assets/img.png", "daily-image.png"):
        p = os.path.join(hist, rel)
        assert os.path.exists(p), rel
        assert not (os.stat(p).st_mode & 0o222), f"{rel} should be read-only"


@pytest.mark.asyncio
async def test_retention_prunes_oldest(fresh_db, monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    Session, _ = fresh_db
    async with Session() as db:
        task = Task(owner_id="local", name="t", prompt_template="x", asset_max_count=2)
        db.add(task)
        await db.commit()
        await db.refresh(task)

        # Three runs, one asset each; files on disk under outputs.
        for i in range(1, 4):
            run = Run(owner_id="local", task_id=task.id, status="succeeded")
            db.add(run)
            await db.commit()
            await db.refresh(run)
            rel = f"assets/img{i}.png"
            _write(task_assets.asset_abs_path(run.id, rel), b"data")
            db.add(RunAsset(run_id=run.id, rel_path=rel, mime="image/png", bytes=4))
            await db.commit()

        pruned = await task_assets.enforce_retention(db, task)
        await db.commit()

        assert pruned == 1
        usage = await task_assets.storage_usage(db, "local", task_id=task.id)
        assert usage["count"] == 2
        # oldest file gone
        first = (await db.execute(
            __import__("sqlalchemy").select(Run).where(Run.task_id == task.id).order_by(Run.id)
        )).scalars().first()
        assert not os.path.exists(task_assets.asset_abs_path(first.id, "assets/img1.png"))


@pytest.mark.asyncio
async def test_clear_task_assets(fresh_db, monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    Session, _ = fresh_db
    async with Session() as db:
        task = Task(owner_id="local", name="t", prompt_template="x")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        run = Run(owner_id="local", task_id=task.id, status="succeeded")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        rel = "assets/img.png"
        _write(task_assets.asset_abs_path(run.id, rel), b"data")
        db.add(RunAsset(run_id=run.id, rel_path=rel, mime="image/png", bytes=4))
        await db.commit()

        n = await task_assets.clear_task_assets(db, task)
        await db.commit()
        assert n == 1
        assert (await task_assets.storage_usage(db, "local"))["count"] == 0
        assert not os.path.exists(task_assets.asset_abs_path(run.id, rel))


@pytest.mark.asyncio
async def test_import_referenced_assets_from_agent_home(monkeypatch, tmp_path):
    """An agy-style report referencing an image saved in the agent HOME (outside the
    run cwd) gets the file pulled in + the reference rewritten to the captured path."""
    _settings_to(monkeypatch, tmp_path)
    # Stand in for the sandbox HOME the agent saved into (batond-readable here, so the
    # dev fallback in read_file_as_agent reads it directly — no spawner in tests).
    home = tmp_path / "home"
    monkeypatch.setattr(task_workspace._settings, "sandbox_home", str(home), raising=False)
    brain = home / ".gemini" / "antigravity-cli" / "brain" / "abc"
    img = brain / "ai_ecosystem_map_123.jpg"
    _write(str(img), b"\xff\xd8\xffJPEGDATA")

    text = (
        f"# Report\n\n![AI Ecosystem Map]({img})\n\n"
        f"See also [the source](file://{home}/.gemini/antigravity-cli/brain/abc/ai_ecosystem_map_123.jpg).\n"
        "And an external [link](https://example.com/x.png) that must NOT be pulled.\n"
        "Plus a forbidden ref to /etc/shadow.jpg that must NOT be pulled.\n"
    )
    outputs = os.path.join(str(tmp_path / "outputs"), "run_7")
    os.makedirs(outputs, exist_ok=True)

    new_text, captured = await task_workspace.import_referenced_assets(text, outputs)

    assert len(captured) == 1
    rel = captured[0]["rel_path"]
    assert rel == "assets/ai_ecosystem_map_123.jpg"
    assert captured[0]["mime"] == "image/jpeg"
    assert os.path.exists(os.path.join(outputs, rel))
    # Both the markdown image ref and the file:// ref are rewritten to the rel path.
    assert str(img) not in new_text
    assert f"file://{home}" not in new_text
    assert new_text.count(rel) == 2
    # External + out-of-root refs untouched / not pulled.
    assert "https://example.com/x.png" in new_text
    assert "/etc/shadow.jpg" in new_text


@pytest.mark.asyncio
async def test_read_file_as_agent_large_file_no_deadlock(monkeypatch, tmp_path):
    """Reading via the spawner must drain the pipe — a >64K file (the image case)
    would deadlock or truncate with a single read()+wait() (the run-#41 hang)."""
    import stat as _stat

    from app import sandbox

    # A stand-in 'spawner' that drops the leading "--" and execs the rest (cat <path>).
    shim = tmp_path / "spawn_shim.sh"
    shim.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    shim.chmod(shim.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    monkeypatch.setattr(sandbox._settings, "sandbox_spawn_path", str(shim), raising=False)
    assert sandbox.available()

    big = tmp_path / "big.bin"
    payload = os.urandom(512 * 1024)  # 512K, well over the ~64K pipe buffer
    big.write_bytes(payload)

    data = await asyncio.wait_for(
        sandbox.read_file_as_agent(str(big), max_bytes=100 * 1024 * 1024), timeout=15
    )
    assert data == payload

    # Over-cap files are rejected, not truncated.
    none = await asyncio.wait_for(
        sandbox.read_file_as_agent(str(big), max_bytes=1024), timeout=15
    )
    assert none is None


def test_asset_abs_path_rejects_traversal(monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    assert task_assets.asset_abs_path(1, "../../etc/passwd") is None
    assert task_assets.asset_abs_path(1, "assets/ok.png") is not None
