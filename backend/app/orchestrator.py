"""
orchestrator.py — Run lifecycle: route → stream → persist → output (§9).

execute_run(run_id):
  1. Load Run+Task; status=planning; broadcast
  2. Route: router.resolve → ordered candidate list or DeferredResult
  3. Build prompt from template + params
  4. Failover loop: for each candidate, run executor; on rate-limit → mark_cooldown + advance;
     on success → break; if all exhausted → overflow_to or deferred
  5. Post-process: extract JSON block → .json file; write .md; set summary/totals
  6. status=succeeded / failed / deferred; broadcast

enqueue_run(task_id, trigger): create Run(status=queued), create_task(execute_run), return.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app import task_workspace
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.logging_config import bind_run
from app.models import Run, RunEvent, Task
from app.providers.base import EventKind, ExecEvent, ExecResult, Usage
from app.providers.registry import get_executor
from app.quota import quota_tracker
from app.router import DeferredResult, resolve
from app.schemas import RunOut
from app.ws import ws_manager

logger = logging.getLogger(__name__)
_settings = get_settings()

# Per-provider semaphores (created lazily)
_provider_sems: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(_settings.per_provider_concurrency)
)
_global_sem = asyncio.Semaphore(_settings.max_concurrent_runs)

# Active run cancel handles
_cancel_handles: dict[int, asyncio.Task] = {}


# ── Run lifecycle ─────────────────────────────────────────────────────────────

async def execute_run(run_id: int) -> None:
    """Main lifecycle. Safe to run as a background asyncio.Task."""
    async with AsyncSessionLocal() as db:
        run = await db.get(Run, run_id)
        if run is None:
            logger.error("execute_run: run %d not found", run_id)
            return
        task = await db.get(Task, run.task_id)
        if task is None:
            await _fail(db, run, "Task not found")
            return
        owner_id = run.owner_id

    with bind_run(run_id, owner_id):
        async with _global_sem:
            await _do_execute(run_id, task)


async def _do_execute(run_id: int, task: Task) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return

        # ── 1. Planning ──────────────────────────────────────────────────────
        run.status = "planning"
        run.started_at = datetime.now(UTC)
        await db.commit()
        await _broadcast_run(run)

        # ── 2. Route ─────────────────────────────────────────────────────────
        routing = task.routing or {}

        # D-0016 cron rule (P-0019): scheduled runs ride the headless `cli -p`
        # lane. Providers without a documented headless mode (currently {grok})
        # are filtered out of the scheduled candidate rotation by default;
        # cron_allow_no_headless_providers opts the user into the ToS risk
        # (personal/self-host). Manual runs aren't affected.
        if run.trigger == "schedule" and not _settings.cron_allow_no_headless_providers:
            from app.providers.registry import is_headless_capable
            raw_candidates = list(routing.get("candidates") or _settings.candidates_list)
            cron_candidates = [c for c in raw_candidates if is_headless_capable(c)]
            dropped = [c for c in raw_candidates if c not in cron_candidates]
            if dropped:
                logger.info(
                    "[orchestrator] run %d (scheduled): filtered no-headless "
                    "candidates %s (D-0016)",
                    run_id, dropped,
                )
                await _emit_event(
                    db, run, EventKind.route, "cron_no_headless_filter",
                    data={"dropped": dropped,
                          "reason": "no headless `cli -p` mode (D-0016/P-0019)"},
                )
                # Rebuild routing with the filtered list. If the task pinned
                # candidates via routing, preserve everything else.
                routing = {**routing, "candidates": cron_candidates}

        # Budget gate (P-0009 #2): if the owner is over the daily cap, tell the
        # router to degrade to zero-marginal-cost providers instead of spending.
        from app.cost import over_daily_budget
        degrade = await over_daily_budget(db, run.owner_id)
        if degrade:
            await _emit_event(db, run, EventKind.route, "budget_degraded",
                              data={"daily_budget_usd": _settings.daily_budget_usd})
            logger.info(
                "[orchestrator] run %d over daily budget — degrading to free providers", run_id
            )
        route_result = resolve(routing, quota_tracker, degrade_to_free=degrade)

        if isinstance(route_result, DeferredResult):
            run.status = "deferred"
            run.deferred_until = route_result.deferred_until
            if route_result.cooling_providers:
                run.error = f"All candidates cooling: {route_result.cooling_providers}"
            elif degrade:
                run.error = (
                    f"Over daily budget (${_settings.daily_budget_usd:.2f}) and no "
                    f"zero-cost provider (plan-CLI/local) available — deferred."
                )
            else:
                routing_candidates = routing.get("candidates", _settings.candidates_list)
                run.error = (
                    f"No candidates matched routing policy "
                    f"(candidates={routing_candidates}, "
                    f"tags={routing.get('capability_tags', [])})"
                )
            await db.commit()
            await _broadcast_run(run)
            deferred_until = run.deferred_until.isoformat() if run.deferred_until else None
            await _emit_event(db, run, EventKind.route, "deferred",
                              data={"cooling": route_result.cooling_providers,
                                    "deferred_until": deferred_until})
            logger.info("[orchestrator] run %d deferred until %s", run_id, run.deferred_until)
            return

        candidates = route_result.candidates
        overflow_to = route_result.overflow_to

        await _emit_event(db, run, EventKind.route, "routed",
                          data={"candidates": candidates, "overflow_to": overflow_to})

        # ── 3. Build prompt ──────────────────────────────────────────────────
        prompt = _render_prompt(task)
        # Inject the previous run's output so monitoring/diff tasks can reason
        # about what changed, without the agent browsing the filesystem (P-0022).
        prior = task_workspace.latest_history(task.id)
        if prior:
            prompt = (
                f"{prompt}\n\n---\nFor reference, the previous run of this task produced "
                f"the following output. Note what has changed since then:\n\n{prior}\n---"
            )

        # ── 4. Failover loop ─────────────────────────────────────────────────
        attempts: list[dict] = []
        final_result: ExecResult | None = None
        final_usage = Usage()
        subagents = 0
        tool_calls = 0

        # Per-task isolated workspace: the agent (running as `sandbox`) cwd's into
        # a fresh current/ scratch and cannot reach /app or control-plane /data
        # (P-0022/D-0020). Canonical outputs are copied to /data/outputs/run_<id>
        # and promoted to read-only history/ after success.
        workdir = task_workspace.prepare_current(task.id)
        outputs_dir = os.path.join(_settings.outputs_dir, f"run_{run_id}")
        os.makedirs(outputs_dir, exist_ok=True)

        for provider_name in candidates:
            executor = get_executor(provider_name)
            if executor is None:
                logger.warning("[orchestrator] executor %s not available, skipping", provider_name)
                attempts.append({"provider": provider_name, "outcome": "unavailable"})
                continue

            sem = _provider_sems[provider_name]
            async with sem:
                run.status = "running"
                run.provider = provider_name
                await db.commit()
                await _broadcast_run(run)

                attempt: dict[str, Any] = {"provider": provider_name, "outcome": "pending"}
                attempts.append(attempt)
                run.attempts = list(attempts)
                await db.commit()

                rate_limited = False
                error_msg: str | None = None
                seq = await _next_seq(db, run_id)

                try:
                    async for ev in executor.run_stream(
                        prompt,
                        workdir=workdir,
                        tools_enabled=_settings.autonomous_tools,
                        max_rounds=10,
                        budget_usd=1.0,
                    ):
                        # Persist non-token events
                        if ev.kind != EventKind.token:
                            await _emit_event(db, run, ev.kind, ev.phase or ev.message,
                                              data=ev.data, seq_override=seq)
                            seq += 1

                        # Broadcast all events to WS
                        await _broadcast_event(run_id, ev, seq)

                        # Count subagents and tool calls
                        if ev.kind == EventKind.subagent:
                            subagents += 1
                        elif ev.kind == EventKind.tool:
                            tool_calls += 1

                        # Terminal events
                        if ev.kind == EventKind.result:
                            final_result = ev.data.get("result")
                            usage_raw = ev.data.get("usage", {})
                            final_usage = Usage(
                                tokens_in=usage_raw.get("tokens_in", 0),
                                tokens_out=usage_raw.get("tokens_out", 0),
                                cost_usd=usage_raw.get("cost_usd", 0.0),
                            )
                            attempt["outcome"] = "success"
                            break

                        elif ev.kind == EventKind.error:
                            msg = ev.message or ""
                            if ev.data.get("rate_limit") or "rate_limit_reached" in msg:
                                rate_limited = True
                                reset_at_str = ev.data.get("reset_at")
                                reset_at = None
                                if reset_at_str:
                                    try:
                                        reset_at = datetime.fromisoformat(reset_at_str)
                                    except ValueError:
                                        pass
                                quota_tracker.mark_cooldown(provider_name, reset_at)
                                attempt["outcome"] = "rate_limited"
                                attempt["reset_at"] = reset_at_str
                                await _broadcast_event(run_id, ExecEvent(
                                    kind=EventKind.route,
                                    message=f"{provider_name} rate-limited; trying next",
                                    data={"provider": provider_name, "cooling": True,
                                          "next": candidates[candidates.index(provider_name) + 1:]
                                          if provider_name in candidates else []}
                                ), seq)
                            else:
                                error_msg = msg
                                attempt["outcome"] = "error"
                            break

                except Exception as exc:
                    logger.exception(
                        "[orchestrator] run %d provider %s error", run_id, provider_name
                    )
                    attempt["outcome"] = "error"
                    error_msg = str(exc)

                run.attempts = list(attempts)
                await db.commit()

                if final_result is not None:
                    break  # success — exit failover loop

                if rate_limited:
                    continue  # try next candidate

                if error_msg and routing.get("failover", True):
                    continue  # non-quota error but failover enabled

                # Hard error with failover=False
                if error_msg:
                    await _fail(db, run, error_msg)
                    return

        # ── Overflow ──────────────────────────────────────────────────────────
        if final_result is None and overflow_to:
            executor = get_executor(overflow_to)
            if executor:
                logger.info("[orchestrator] run %d using overflow provider %s", run_id, overflow_to)
                async with _provider_sems[overflow_to]:
                    seq = await _next_seq(db, run_id)
                    async for ev in executor.run_stream(prompt, workdir=workdir,
                                                        tools_enabled=_settings.autonomous_tools,
                                                        max_rounds=10, budget_usd=1.0):
                        if ev.kind != EventKind.token:
                            await _emit_event(db, run, ev.kind, ev.phase or ev.message,
                                              data=ev.data, seq_override=seq)
                            seq += 1
                        await _broadcast_event(run_id, ev, seq)
                        if ev.kind == EventKind.result:
                            final_result = ev.data.get("result")
                            usage_raw = ev.data.get("usage", {})
                            final_usage = Usage(
                                **{
                                    k: usage_raw.get(k, 0)
                                    for k in ("tokens_in", "tokens_out", "cost_usd")
                                }
                            )
                            run.overflow_used = True
                            break

        # ── All exhausted → deferred ──────────────────────────────────────────
        if final_result is None:
            run.status = "deferred"
            run.deferred_until = quota_tracker.earliest_reset(candidates)
            run.attempts = attempts
            await db.commit()
            await _broadcast_run(run)
            return

        # ── 5. Post-process outputs ───────────────────────────────────────────
        full_text = final_result.text if final_result else ""
        json_block: str | None = None

        # Agent-written files: CLI agents (grok, agy) sometimes use file_write to
        # save the actual report and only print a short summary (or, for agy, just
        # planning narration) in their final text. The workdir is the agent's own
        # per-run scratch (our canonical output.md lives in outputs_dir), so scan
        # it for the largest agent-written .md — including output.md, the filename
        # agents most often pick — and prefer it when it dwarfs the CLI final text.
        agent_md = _find_best_agent_md(workdir)
        if agent_md and len(agent_md) > max(len(full_text) * 2, 500):
            logger.info(
                "[orchestrator] using agent-written .md (%d bytes) over CLI final text (%d bytes)",
                len(agent_md), len(full_text),
            )
            full_text = agent_md

        # Canonical outputs land in the control-plane outputs dir (batond-owned),
        # not the agent's sandbox workspace, then get promoted to read-only history.
        if task.want_json:
            json_block = _extract_json_block(full_text)
            if json_block:
                json_path = os.path.join(outputs_dir, "output.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    f.write(json_block)
                run.json_path = json_path
                full_text = _strip_json_block(full_text)

        if task.want_markdown:
            md_path = os.path.join(outputs_dir, "output.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(full_text)
            run.markdown_path = md_path

        # Promote this run's output into the task's read-only history so the next
        # run can read it (injected into its prompt) but cannot mutate it (P-0022).
        task_workspace.promote(task.id, run_id, outputs_dir)


        # ── 6. Finalise ───────────────────────────────────────────────────────
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        run.summary = full_text[:500].strip() if full_text else ""
        run.tokens_in = final_usage.tokens_in
        run.tokens_out = final_usage.tokens_out
        run.cost_usd = final_usage.cost_usd
        run.subagents = subagents
        run.tool_calls = tool_calls
        run.model = final_result.model if final_result else None
        run.tier = _get_tier(run.provider or "")
        run.attempts = attempts

        quota_tracker.record_invocation(
            run.provider or "unknown", final_usage.tokens_in + final_usage.tokens_out
        )

        await db.commit()
        await _broadcast_run(run)
        logger.info(
            "[orchestrator] run %d succeeded via %s (%.4f USD)", run_id, run.provider, run.cost_usd
        )


# ── startup reaper (D-0021) ─────────────────────────────────────────────────────

async def reap_orphaned_runs() -> int:
    """Reconcile runs stranded by a backend restart; return how many were reaped.

    Runs execute as in-memory fire-and-forget asyncio tasks (no durable queue), so a
    crash/restart leaves any `running` or `queued` run with no executor — it would sit
    non-terminal forever. On startup we mark these `failed` with a clear reason so the
    state is honest and the user can requeue (P5: tasks are the real-work unit). Note:
    `deferred` runs are intentionally left alone — the scheduler's deferred-sweep owns
    those. Durable queueing/auto-requeue is a later managed-scale graduation (D-0021).
    """
    now = datetime.now(UTC)
    reaped = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Run).where(Run.status.in_(("running", "queued")))
        )
        for run in result.scalars().all():
            run.status = "failed"
            run.error = "interrupted by backend restart (reaped at startup)"
            run.finished_at = now
            reaped += 1
        if reaped:
            await db.commit()
    if reaped:
        logger.warning("reaped %d orphaned run(s) on startup", reaped)
    return reaped


# ── enqueue ────────────────────────────────────────────────────────────────────

async def enqueue_run(task_id: int, trigger: str = "manual") -> Run:
    """Create a queued Run and fire-and-forget execute_run as a background task."""
    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        run = Run(
            owner_id=task.owner_id,
            task_id=task_id,
            trigger=trigger,
            status="queued",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    bg_task = asyncio.create_task(execute_run(run_id))
    _cancel_handles[run_id] = bg_task
    bg_task.add_done_callback(lambda t: _cancel_handles.pop(run_id, None))

    async with AsyncSessionLocal() as db:
        run = await db.get(Run, run_id)
        return run


async def cancel_run(run_id: int) -> bool:
    """Cancel a running task if possible."""
    task = _cancel_handles.get(run_id)
    if task and not task.done():
        task.cancel()
        async with AsyncSessionLocal() as db:
            run = await db.get(Run, run_id)
            if run:
                run.status = "cancelled"
                run.finished_at = datetime.now(UTC)
                await db.commit()
                await _broadcast_run(run)
        return True
    return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _render_prompt(task: Task) -> str:
    """Render prompt_template with task params, using defaultdict for missing keys."""
    template = task.prompt_template or ""
    params = task.params or {}

    class _DefaultDict(defaultdict):
        def __missing__(self, key):
            return f"{{{key}}}"

    try:
        return template.format_map(_DefaultDict(str, params))
    except Exception:
        return template


def _extract_json_block(text: str) -> str | None:
    """Extract the last fenced ```json block from the output."""
    matches = re.findall(r"```json\s*(.*?)```", text, re.DOTALL)
    return matches[-1].strip() if matches else None


def _find_best_agent_md(workdir: str, exclude: str | None = None) -> str | None:
    """
    Scan workdir for .md files written by the agent. Returns the content of the
    largest one, or None if none found.

    CLI agents like grok/agy often use file_write to persist the actual report and
    only print a short summary (or, for agy, just planning narration) in their
    final text. This recovers the real content without changing the agent's prompt.

    Post-P-0022, `workdir` is the agent's own per-run `current/` scratch and our
    canonical output.md is written to the separate outputs dir — so every .md here
    is agent-authored, *including* `output.md`, which is the most natural filename
    an agent picks ("…write the report to output.md"). It must therefore NOT be
    excluded: excluding it discarded exactly the report we wanted and let the
    plain-text narration become the deliverable. `exclude` is kept optional only
    for callers that still share a dir with our own writes.
    """
    import glob
    pattern = os.path.join(workdir, "*.md")
    candidates = [
        p for p in glob.glob(pattern)
        if exclude is None or os.path.basename(p) != exclude
    ]
    if not candidates:
        return None
    # Pick the largest file — most likely to be the actual report
    best = max(candidates, key=os.path.getsize)
    try:
        with open(best, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None




_HEADING_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]")


def _strip_empty_sections(text: str) -> str:
    """
    Remove Markdown headings whose section has no content.

    After a ```json block is extracted, the heading that introduced it (e.g.
    "## Structured Data (JSON)") is left dangling with nothing beneath it, making
    the report look truncated. A heading is "empty" when, scanning forward until
    the next heading of the same-or-higher level (or EOF), there is no non-blank,
    non-heading text. Parent headings with real content are preserved; empty
    nested headings collapse together.
    """
    lines = text.split("\n")
    n = len(lines)
    is_heading = [bool(_HEADING_RE.match(ln)) for ln in lines]
    level = [
        len(_HEADING_RE.match(lines[i]).group(1)) if is_heading[i] else 0
        for i in range(n)
    ]
    has_content = [
        (not is_heading[i] and lines[i].strip() != "") for i in range(n)
    ]

    remove = [False] * n
    for i in range(n):
        if not is_heading[i]:
            continue
        lvl = level[i]
        j = i + 1
        content = False
        while j < n and not (is_heading[j] and level[j] <= lvl):
            if has_content[j]:
                content = True
                break
            j += 1
        if not content:
            remove[i] = True

    kept = [lines[i] for i in range(n) if not remove[i]]
    return "\n".join(kept)


def _strip_json_block(text: str) -> str:
    """Remove fenced ```json blocks and any heading/separator they left dangling."""
    text = re.sub(r"```json\s*.*?```", "", text, flags=re.DOTALL)
    text = _strip_empty_sections(text)
    # Collapse the blank-line runs left behind so the report reads cleanly.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Trim trailing thematic breaks (---, ***, ___) the model placed before the
    # now-removed JSON section, so the report doesn't end on a dangling separator.
    text = re.sub(
        r"(?:\s*^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$)+\s*\Z", "", text, flags=re.MULTILINE
    )
    return text.strip()


def _get_tier(instance_id: str) -> str:
    """Resolve an instance id (e.g. "claude:work") to its template tier."""
    from app.providers.registry import get_instance, get_provider_def
    inst = get_instance(instance_id)
    template = inst.template if inst else instance_id
    pdef = get_provider_def(template)
    return pdef.tier if pdef else "unknown"


async def _fail(db, run: Run, error: str) -> None:
    run.status = "failed"
    run.error = error
    run.finished_at = datetime.now(UTC)
    await db.commit()
    await _broadcast_run(run)


async def _next_seq(db, run_id: int) -> int:
    result = await db.execute(
        select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq.desc()).limit(1)
    )
    last = result.scalar_one_or_none()
    return (last.seq + 1) if last else 0


async def _emit_event(
    db, run: Run, kind: EventKind, message: str, *,
    data: dict | None = None, seq_override: int | None = None, phase: str = ""
) -> None:
    seq = seq_override if seq_override is not None else await _next_seq(db, run.id)
    # Strip non-JSON-serializable objects (ExecResult dataclasses) before DB persistence
    safe_data = None
    if data:
        safe_data = {k: v for k, v in data.items() if not isinstance(v, ExecResult)}
    ev = RunEvent(
        run_id=run.id,
        seq=seq,
        kind=kind.value,
        phase=phase or message[:64],
        message=message,
        data=safe_data,
    )
    db.add(ev)
    await db.commit()


async def _broadcast_run(run: Run) -> None:
    try:
        payload = {"type": "run.update", "run": RunOut.model_validate(run).model_dump(mode="json")}
        await ws_manager.broadcast(payload)
    except Exception as exc:
        logger.debug("WS broadcast run error: %s", exc)


async def _broadcast_event(run_id: int, ev: ExecEvent, seq: int) -> None:
    try:
        payload = {
            "type": "run.event",
            "run_id": run_id,
            "event": {
                "seq": seq,
                "kind": ev.kind.value,
                "phase": ev.phase,
                "message": ev.message,
                "text": ev.text,
                "data": {k: v for k, v in (ev.data or {}).items() if not isinstance(v, ExecResult)},
            },
        }
        await ws_manager.broadcast(payload)
    except Exception as exc:
        logger.debug("WS broadcast event error: %s", exc)
