"""
scheduler.py — APScheduler AsyncIOScheduler integration (§11).

Manages cron/interval jobs for enabled tasks and a deferred-run sweep.
Wired into main.py lifespan.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db import AsyncSessionLocal
from app.models import Run, Task

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


# Expose as module-level singleton (for import by main.py)
scheduler_instance = None  # populated in start_scheduler


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def start_scheduler() -> None:
    """Start the scheduler and sync all tasks. Called from lifespan."""
    global scheduler_instance
    sched = _get_scheduler()

    # Recurring deferred-run sweep: every 60s re-enqueue runs whose deferred_until has passed
    sched.add_job(
        _sweep_deferred_runs,
        trigger=IntervalTrigger(seconds=60),
        id="__deferred_sweep__",
        replace_existing=True,
        misfire_grace_time=30,
    )

    await _sync_all_tasks(sched)

    sched.start()
    scheduler_instance = _SchedulerProxy(sched)
    logger.info("[scheduler] started; %d jobs", len(sched.get_jobs()))


async def stop_scheduler() -> None:
    """Gracefully stop the scheduler. Called from lifespan shutdown."""
    sched = _get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("[scheduler] stopped")


# ── Proxy object for main.py access ──────────────────────────────────────────

class _SchedulerProxy:
    """Thin wrapper so main.py can call scheduler_instance.sync_task / remove_task."""

    def __init__(self, sched: AsyncIOScheduler) -> None:
        self._sched = sched

    async def sync_task(self, task: Task) -> None:
        _sync_task(self._sched, task)

    def remove_task(self, task_id: int) -> None:
        job_id = f"task_{task_id}"
        if self._sched.get_job(job_id):
            self._sched.remove_job(job_id)
            logger.info("[scheduler] removed job for task %d", task_id)


# ── Task sync ─────────────────────────────────────────────────────────────────

async def _sync_all_tasks(sched: AsyncIOScheduler) -> None:
    """Load all tasks from DB and register their jobs."""
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(select(Task).where(Task.enabled.is_(True)))
        tasks = result.scalars().all()

    for task in tasks:
        _sync_task(sched, task)

    logger.info("[scheduler] synced %d enabled tasks", len(tasks))


def _sync_task(sched: AsyncIOScheduler, task: Task) -> None:
    """Register (or replace/remove) a job for this task."""
    job_id = f"task_{task.id}"

    # Remove existing job first
    if sched.get_job(job_id):
        sched.remove_job(job_id)

    if not task.enabled or task.schedule_kind == "none":
        return

    trigger = None
    if task.schedule_kind == "cron" and task.schedule_expr:
        # Interpret the cron expression in the task's timezone (DST-aware), so a
        # user can author "0 7 * * *" meaning 7am local without translating to UTC.
        tz = getattr(task, "timezone", None) or "UTC"
        try:
            trigger = CronTrigger.from_crontab(task.schedule_expr, timezone=tz)
        except Exception as exc:
            logger.warning("[scheduler] invalid cron expr/timezone for task %d: %s (tz=%s) — %s",
                           task.id, task.schedule_expr, tz, exc)
            # Fall back to UTC rather than silently dropping the schedule.
            try:
                trigger = CronTrigger.from_crontab(task.schedule_expr, timezone="UTC")
            except Exception:
                return
    elif task.schedule_kind == "interval" and task.schedule_expr:
        try:
            seconds = int(task.schedule_expr)
            trigger = IntervalTrigger(seconds=seconds)
        except ValueError:
            logger.warning("[scheduler] invalid interval for task %d: %s",
                           task.id, task.schedule_expr)
            return

    if trigger is None:
        return

    task_id = task.id  # capture for closure

    async def _run_task():
        from app.orchestrator import enqueue_run
        try:
            await enqueue_run(task_id, trigger="schedule")
        except Exception as exc:
            logger.exception("[scheduler] failed to enqueue task %d: %s", task_id, exc)

    sched.add_job(
        _run_task,
        trigger=trigger,
        id=job_id,
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("[scheduler] registered job for task %d (%s %s)",
                task.id, task.schedule_kind, task.schedule_expr)


# ── Deferred run sweep ────────────────────────────────────────────────────────

async def _sweep_deferred_runs() -> None:
    """Re-enqueue any deferred runs whose deferred_until has passed."""
    from sqlalchemy import select

    from app.orchestrator import enqueue_run

    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Run).where(
                Run.status == "deferred",
                Run.deferred_until <= now,
            )
        )
        runs = result.scalars().all()

    for run in runs:
        logger.info("[scheduler] re-enqueueing deferred run %d (task %d)", run.id, run.task_id)
        try:
            # Create a fresh run for the same task rather than retrying the old Run
            from app.orchestrator import enqueue_run
            await enqueue_run(run.task_id, trigger="schedule")
            # Mark old deferred run as cancelled to avoid duplicate re-enqueues
            async with AsyncSessionLocal() as db:
                old = await db.get(Run, run.id)
                if old and old.status == "deferred":
                    old.status = "cancelled"
                    old.error = "superseded by deferred sweep re-enqueue"
                    await db.commit()
        except Exception as exc:
            logger.exception("[scheduler] failed to re-enqueue deferred run %d: %s", run.id, exc)
