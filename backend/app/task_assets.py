"""
task_assets.py — run-asset persistence, retention, storage accounting (P-0050/D-0046).

Task runs can produce non-text artifacts (generated images, agent-written csv/pdf).
`task_workspace.capture_assets` copies them into the run's canonical outputs dir; this
module records them as `RunAsset` rows, enforces per-task retention caps, exposes
storage usage, and handles delete/clear.

Storage is accounted from `RunAsset.bytes` (the canonical outputs copy). Files live at
`<outputs_dir>/run_<run_id>/<rel_path>`; `asset_abs_path` resolves that traversal-safe.
The read-only history copy (task_workspace.promote) is best-effort cleaned on delete.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat

from sqlalchemy import func, select

from app.config import get_settings
from app.models import Run, RunAsset, Task

logger = logging.getLogger(__name__)
_settings = get_settings()


def asset_abs_path(run_id: int, rel_path: str) -> str | None:
    """Resolve a run asset's absolute path under its outputs dir, refusing traversal."""
    base = os.path.realpath(os.path.join(_settings.outputs_dir, f"run_{int(run_id)}"))
    target = os.path.realpath(os.path.join(base, rel_path))
    if not (target == base or target.startswith(base + os.sep)):
        return None
    return target


def record_assets(run: Run, captured: list[dict]) -> list[RunAsset]:
    """Build RunAsset rows for the captured artifacts (caller adds + commits)."""
    rows = [
        RunAsset(
            run_id=run.id,
            rel_path=c["rel_path"],
            mime=c.get("mime"),
            bytes=int(c.get("bytes", 0)),
        )
        for c in captured
    ]
    return rows


def _unlink(path: str | None) -> None:
    """Remove a (possibly read-only 0440) file, best-effort."""
    if not path or not os.path.exists(path):
        return
    try:
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning("[task_assets] could not remove %s: %s", path, exc)


def _delete_asset_files(asset: RunAsset, task_id: int | None) -> None:
    """Delete an asset's canonical outputs file and its read-only history copy."""
    _unlink(asset_abs_path(asset.run_id, asset.rel_path))
    if task_id is not None:
        from app import task_workspace

        try:
            hist = os.path.join(
                task_workspace._task_root(task_id),  # noqa: SLF001 — same package
                "history",
                f"run_{asset.run_id}",
                asset.rel_path,
            )
            _unlink(hist)
        except ValueError:
            pass


async def storage_usage(db, owner_id: str, task_id: int | None = None) -> dict:
    """Aggregate stored run-asset {count, bytes} for an owner, optionally one task."""
    q = (
        select(func.count(RunAsset.id), func.coalesce(func.sum(RunAsset.bytes), 0))
        .join(Run, RunAsset.run_id == Run.id)
        .where(Run.owner_id == owner_id)
    )
    if task_id is not None:
        q = q.where(Run.task_id == task_id)
    count, total = (await db.execute(q)).one()
    return {"count": int(count or 0), "bytes": int(total or 0)}


async def enforce_retention(db, task: Task) -> int:
    """Prune the oldest run assets of a task until it's under its caps; return #pruned.

    NULL caps = unlimited (no-op). Caller commits. Deletes whole assets (oldest run
    first) — never partial — until both count and byte budgets are satisfied.
    """
    if task.asset_max_count is None and task.asset_max_bytes is None:
        return 0

    rows = (
        await db.execute(
            select(RunAsset)
            .join(Run, RunAsset.run_id == Run.id)
            .where(Run.task_id == task.id)
            .order_by(RunAsset.id.asc())  # oldest first
        )
    ).scalars().all()

    total_count = len(rows)
    total_bytes = sum(a.bytes for a in rows)
    pruned = 0
    for asset in rows:
        over_count = task.asset_max_count is not None and total_count > task.asset_max_count
        over_bytes = task.asset_max_bytes is not None and total_bytes > task.asset_max_bytes
        if not (over_count or over_bytes):
            break
        _delete_asset_files(asset, task.id)
        await db.delete(asset)
        total_count -= 1
        total_bytes -= asset.bytes
        pruned += 1
    if pruned:
        logger.info("[task_assets] retention pruned %d asset(s) for task %d", pruned, task.id)
    return pruned


async def delete_run_assets(db, run: Run) -> int:
    """Delete all stored assets for one run (files + rows). Caller commits."""
    rows = (
        await db.execute(select(RunAsset).where(RunAsset.run_id == run.id))
    ).scalars().all()
    for asset in rows:
        _delete_asset_files(asset, run.task_id)
        await db.delete(asset)
    return len(rows)


async def clear_task_assets(db, task: Task) -> int:
    """Delete all stored assets across every run of a task (files + rows)."""
    runs = (
        await db.execute(select(Run).where(Run.task_id == task.id))
    ).scalars().all()
    n = 0
    for run in runs:
        n += await delete_run_assets(db, run)
    # Drop now-empty asset subdirs from the canonical outputs dirs (best-effort).
    for run in runs:
        run_dir = os.path.join(_settings.outputs_dir, f"run_{run.id}")
        for sub in ("assets", "data"):
            shutil.rmtree(os.path.join(run_dir, sub), ignore_errors=True)
    return n
