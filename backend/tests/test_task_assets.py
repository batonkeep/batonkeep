"""
tests/test_task_assets.py — task-run generated-asset handling (P-0050/D-0046).

Covers the capture → promote → retention/storage pipeline that keeps a task run's
non-text artifacts (generated images, agent-written csv/pdf) instead of discarding
them with the scratch.
"""
from __future__ import annotations

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
    monkeypatch.setitem(task_workspace._settings.__dict__, "work_dir", str(tmp_path / "work"))
    monkeypatch.setitem(task_workspace._settings.__dict__, "outputs_dir", str(tmp_path / "outputs"))
    # task_assets imports the same cached settings singleton, but set explicitly in
    # case that ever changes.
    monkeypatch.setitem(task_assets._settings.__dict__, "outputs_dir", str(tmp_path / "outputs"))


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_capture_picks_assets_and_data_skips_scratch(monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    workdir = task_workspace.prepare_current(1)
    _write(os.path.join(workdir, "assets", "generated-1.png"), b"\x89PNG")
    _write(os.path.join(workdir, "data", "report.csv"), b"a,b\n1,2\n")
    _write(os.path.join(workdir, "scratch.png"), b"junk")   # not under assets/ or data/
    _write(os.path.join(workdir, "assets", "notes.exe"), b"nope")  # not allowlisted

    outputs = os.path.join(str(tmp_path / "outputs"), "run_5")
    os.makedirs(outputs, exist_ok=True)
    captured = task_workspace.capture_assets(workdir, outputs)

    rels = sorted(c["rel_path"] for c in captured)
    assert rels == ["assets/generated-1.png", "data/report.csv"]
    assert os.path.exists(os.path.join(outputs, "assets/generated-1.png"))
    # mime guessed
    by_path = {c["rel_path"]: c for c in captured}
    assert by_path["assets/generated-1.png"]["mime"] == "image/png"
    assert by_path["data/report.csv"]["bytes"] == len(b"a,b\n1,2\n")


def test_promote_copies_assets_readonly(monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    task_workspace.prepare_current(2)
    outputs = os.path.join(str(tmp_path / "outputs"), "run_9")
    _write(os.path.join(outputs, "output.md"), b"# report")
    _write(os.path.join(outputs, "assets", "img.png"), b"\x89PNG")

    task_workspace.promote(2, 9, outputs)
    hist = os.path.join(task_workspace._task_root(2), "history", "run_9")
    asset = os.path.join(hist, "assets", "img.png")
    assert os.path.exists(asset)
    # read-only (0440): no write bit
    assert not (os.stat(asset).st_mode & 0o222)


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


def test_asset_abs_path_rejects_traversal(monkeypatch, tmp_path):
    _settings_to(monkeypatch, tmp_path)
    assert task_assets.asset_abs_path(1, "../../etc/passwd") is None
    assert task_assets.asset_abs_path(1, "assets/ok.png") is not None
