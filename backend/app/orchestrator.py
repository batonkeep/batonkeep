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

from sqlalchemy import select, update

from app import task_assets, task_workspace
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.logging_config import bind_run
from app.models import RoutingDecision, Run, RunEvent, Task
from app.policy import resolve_effective_policy
from app.providers.base import EventKind, ExecEvent, ExecResult, Usage
from app.providers.registry import get_executor
from app.quota import quota_tracker
from app.redact import redact_json, redact_text
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
            try:
                await _do_execute(run_id, task)
            except asyncio.CancelledError:
                # Cancellation is owned by cancel_run (it sets status=cancelled);
                # don't mask it as a failure — just propagate.
                raise
            except Exception as exc:
                # Without this, an unhandled error after the run went non-terminal
                # (e.g. a post-processing crash) would strand it in "running"
                # forever and the fire-and-forget task would swallow the error.
                logger.exception("[orchestrator] run %d crashed in _do_execute", run_id)
                async with AsyncSessionLocal() as db:
                    run = await db.get(Run, run_id)
                    if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
                        await _fail(db, run, f"internal error: {exc}")


async def _do_execute(run_id: int, task: Task) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return

        # ── 1. Claim (idempotency guard) ─────────────────────────────────────
        # Atomically transition queued → planning. If the row is no longer queued
        # (already claimed by another worker, or terminal), this is a no-op and we
        # bail — so a duplicated execute_run (e.g. a stray re-enqueue) can never
        # double-execute the same run. (P-0025 #2)
        now = datetime.now(UTC)
        claim = await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(status="planning", started_at=now)
        )
        await db.commit()
        if claim.rowcount == 0:
            await db.refresh(run)
            logger.warning(
                "[orchestrator] run %d not claimable (status=%s) — skipping to avoid "
                "double-execution", run_id, run.status,
            )
            return
        await db.refresh(run)
        await _broadcast_run(run)

        # Effective declared policy for this run (D-0058 seam 1): every constraint
        # read below goes through the one resolver so Phase C PolicySet inheritance
        # is an implementation swap, not a call-site rewrite.
        policy = resolve_effective_policy(task=task)

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

        # P-0053: persist the routing decision (what was considered + why). Best-effort
        # — telemetry must never break a run, so a failure here is logged and swallowed.
        await _record_routing_decision(db, run, route_result.trace)

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

        # ── 4. Failover loop (with bounded in-process retry, P-0025 #2) ───────
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

        # S0 substrate: project the declared context read-only into the workspace
        # and persist the ContextReceipt before any executor starts (the receipt
        # must survive a crashed run). Per-source problems are recorded as receipt
        # exclusions; an unexpected failure here is loud but doesn't kill the run.
        try:
            from app import project_context

            receipt = await project_context.project_for_execution(
                db,
                owner_id=run.owner_id,
                project_id=run.project_id,
                work_item_id=run.work_item_id,
                workdir=workdir,
                run_id=run_id,
            )
            if receipt is not None:
                await _emit_event(
                    db, run, EventKind.phase, "context_projected",
                    data={
                        "receipt_id": receipt.id,
                        "sources": len(receipt.sources or []),
                        "excluded": len(receipt.exclusions or []),
                        "ledger_sha": receipt.ledger_sha,
                    },
                )
        except Exception:
            logger.exception(
                "[orchestrator] run %d context projection failed — continuing", run_id
            )

        async def _run_candidates() -> tuple[str, str | None]:
            """Run the candidate chain (+ overflow) once.

            Mutates the enclosing final_result/final_usage/subagents/tool_calls
            and the attempts log. Returns (outcome, error_msg) where outcome is:
              "success"   — final_result set, ready to post-process
              "cooling"   — exhausted with rate-limits; a legitimate deferral
              "error"     — exhausted with transient (non-quota) errors; retryable
              "hard_fail" — a non-quota error with failover disabled; terminal
            """
            nonlocal final_result, final_usage, subagents, tool_calls
            rate_limited_any = False
            last_error: str | None = None

            for provider_name in candidates:
                executor = get_executor(provider_name)
                if executor is None:
                    logger.warning(
                        "[orchestrator] executor %s not available, skipping", provider_name
                    )
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
                    # Provenance stamp: the executing CLI version onto the receipt
                    # (last candidate attempted wins on failover — the one whose
                    # output the run records). API-lane executors have no probe.
                    if receipt is not None:
                        probe = getattr(executor, "cli_version", None)
                        version = probe() if callable(probe) else None
                        if version:
                            receipt.cli_version = version
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
                            # P-0046: unattended task — no human to confirm, so
                            # code-exec runs only if the task carries allow-safe/auto.
                            extra={
                                "task": True, "exec_policy": policy.exec_policy,
                                "human_in_loop": False,
                                # P-0046 slice 6: image-gen model override (None → default).
                                "image_model_id": task.image_model_id,
                            },
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
                                    next_up = (
                                        candidates[candidates.index(provider_name) + 1:]
                                        if provider_name in candidates else []
                                    )
                                    await _broadcast_event(run_id, ExecEvent(
                                        kind=EventKind.route,
                                        message=f"{provider_name} rate-limited; trying next",
                                        data={"provider": provider_name, "cooling": True,
                                              "next": next_up},
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

                    # The stream ended without a result, rate-limit, or error event
                    # (e.g. the agent exited after only emitting reasoning/thoughts).
                    # Record it as an explicit error instead of silently leaving the
                    # attempt "pending" and falling through to the next candidate —
                    # that silent fall-through is what made one run quietly execute
                    # the agent again on the next candidate / retry.
                    if final_result is None and not rate_limited and not error_msg:
                        error_msg = "agent stream ended without producing any output"
                        attempt["outcome"] = "error"
                        logger.warning(
                            "[orchestrator] run %d provider %s produced no terminal result",
                            run_id, provider_name,
                        )

                    run.attempts = list(attempts)
                    await db.commit()

                    if final_result is not None:
                        return "success", None

                    if rate_limited:
                        rate_limited_any = True
                        continue  # try next candidate

                    if error_msg:
                        last_error = error_msg
                        if not routing.get("failover", True):
                            return "hard_fail", error_msg
                        continue  # non-quota error but failover enabled

            # ── Overflow ──────────────────────────────────────────────────────
            if final_result is None and overflow_to:
                executor = get_executor(overflow_to)
                if executor:
                    logger.info(
                        "[orchestrator] run %d using overflow provider %s", run_id, overflow_to
                    )
                    async with _provider_sems[overflow_to]:
                        seq = await _next_seq(db, run_id)
                        async for ev in executor.run_stream(
                                prompt, workdir=workdir,
                                tools_enabled=_settings.autonomous_tools,
                                max_rounds=10, budget_usd=1.0,
                                extra={
                                    "task": True, "exec_policy": policy.exec_policy,
                                    "human_in_loop": False,
                                    "image_model_id": task.image_model_id,
                                }):
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

            if final_result is not None:
                return "success", None
            if rate_limited_any:
                return "cooling", None
            return "error", last_error

        # Per-task wall-clock timeout (P-0056/D-0052): bound the elapsed time of the
        # whole candidate/retry drive. A per-task override falls back to the global
        # run_timeout_seconds default. On expiry the in-flight provider stream is
        # cancelled (best-effort) and the run fails honestly with a timeout error.
        # Post-processing (writing outputs) runs *outside* this bound so a finished
        # agent's deliverables are never lost to a late timeout.
        effective_timeout = policy.timeout_seconds

        # Retry wrapper: rerun the chain on transient failure with exponential
        # backoff, bounded by max_run_retries. Rate-limit exhaustion ("cooling")
        # is not retried here — it defers and the scheduler's sweep re-enqueues it.
        max_retries = _settings.max_run_retries
        retry_count = run.retry_count or 0
        try:
            async with asyncio.timeout(effective_timeout):
                while True:
                    outcome, error_msg = await _run_candidates()

                    if outcome == "success":
                        break

                    if outcome == "hard_fail":
                        await _fail(db, run, error_msg or "provider error (failover disabled)")
                        return

                    if outcome == "cooling":
                        run.status = "deferred"
                        run.deferred_until = quota_tracker.earliest_reset(candidates)
                        run.attempts = attempts
                        await db.commit()
                        await _record_routing_outcome(db, run)  # P-0053 slice 2
                        await _broadcast_run(run)
                        return

                    # outcome == "error" → transient; retry if budget remains.
                    if retry_count < max_retries:
                        retry_count += 1
                        run.retry_count = retry_count
                        delay = _settings.retry_backoff_seconds * (2 ** (retry_count - 1))
                        logger.info(
                            "[orchestrator] run %d transient failure (%s); retry %d/%d in %.1fs",
                            run_id, error_msg, retry_count, max_retries, delay,
                        )
                        await _emit_event(
                            db, run, EventKind.route, "retry_scheduled",
                            data={"attempt": retry_count, "max_retries": max_retries,
                                  "delay_seconds": delay, "error": error_msg},
                        )
                        run.attempts = attempts
                        await db.commit()
                        await _broadcast_run(run)
                        await asyncio.sleep(delay)
                        continue

                    # Retries exhausted → terminal failure (the stuck-deferred bug fix:
                    # a non-cooling exhaustion now fails honestly instead of deferring
                    # with a null deferred_until that the sweep can never pick up).
                    plural = "y" if retry_count == 1 else "ies"
                    run.attempts = attempts
                    await _fail(
                        db, run,
                        f"all candidates failed after {retry_count} retr{plural}: {error_msg}",
                    )
                    return
        except TimeoutError:
            # The timeout cancelled whatever was in flight; the session may carry a
            # half-applied transaction, so roll back before recording the failure.
            minutes = effective_timeout / 60
            logger.warning(
                "[orchestrator] run %d exceeded timeout of %ds (%.1f min)",
                run_id, effective_timeout, minutes,
            )
            await db.rollback()
            run = await db.get(Run, run_id)
            if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
                run.attempts = attempts
                await _fail(
                    db, run,
                    f"run timed out after {minutes:g} min "
                    f"({'task limit' if task.timeout_seconds else 'default limit'})",
                )
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

        # Pull in artifacts the agent referenced by absolute path but saved outside
        # the run cwd (antigravity/agy writes generated media to its own HOME, which
        # batond can't read) — copy them into outputs/ via the sandbox helper and
        # rewrite the report's references to the captured relative path so it renders
        # (P-0050/D-0046). Must run before output.md is written. Best-effort: asset
        # handling must never strand or fail a run, so any error is logged and skipped.
        referenced: list[dict] = []
        try:
            full_text, referenced = await task_workspace.import_referenced_assets(
                full_text, outputs_dir
            )
        except Exception:
            logger.exception("[orchestrator] run %d: referenced-asset import failed", run_id)

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

        # Capture non-text deliverables (generated images, agent-written csv/pdf) from
        # the agent's current/ scratch into the canonical outputs dir and record them
        # as RunAsset rows (P-0050/D-0046) — otherwise they'd be discarded with the
        # scratch on the next run. Combine with referenced artifacts pulled from the
        # agent's HOME above (dedupe by rel_path). Then enforce the task's retention caps.
        try:
            captured = task_workspace.capture_assets(workdir, outputs_dir)
        except Exception:
            logger.exception("[orchestrator] run %d: workspace asset capture failed", run_id)
            captured = []
        seen_rel = {c["rel_path"] for c in captured}
        captured += [r for r in referenced if r["rel_path"] not in seen_rel]
        if captured:
            db.add_all(task_assets.record_assets(run, captured))
            await db.flush()

        # Promote this run's output (text + captured assets) into the task's read-only
        # history so the next run can read it (injected into its prompt) but cannot
        # mutate it (P-0022).
        task_workspace.promote(task.id, run_id, outputs_dir)

        await task_assets.enforce_retention(db, task)


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

        # Substrate evidence: index the run's report in the append-only evidence
        # store. Best-effort — evidence must never break the run that produced it.
        if run.project_id and full_text:
            from app import evidence as evidence_store
            await evidence_store.capture_safe(
                db,
                owner_id=run.owner_id,
                project_id=run.project_id,
                work_item_id=run.work_item_id,
                run_id=run_id,
                kind="report",
                filename="output.md",
                text=full_text,
                producer=run.provider or "system",
            )

        quota_tracker.record_invocation(
            run.provider or "unknown", final_usage.tokens_in + final_usage.tokens_out
        )

        # P-0069 tail: verify the bound work item's sub-task contract against the
        # artifacts this run actually produced (the task-lane analog of the session
        # verify — its "committed tree" is the promoted output rel_paths). A verifiable
        # item flips done+verified only when its glob matches a produced artifact;
        # an unmet contract on a succeeded run records an `outputs_missing` advisory.
        # Best-effort — verification must never fail the run that produced the work.
        if run.work_item_id:
            try:
                from app import subtasks as st
                from app.models import WorkItem
                produced = {c["rel_path"] for c in captured}
                if run.markdown_path:
                    produced.add("output.md")
                if run.json_path:
                    produced.add("output.json")
                wi = await db.get(WorkItem, run.work_item_id)
                if wi is not None and wi.subtasks:
                    updated, changed = st.verify(
                        wi.subtasks, produced,
                        source={"lane": "task", "ref": f"run:{run_id}"},
                    )
                    if changed:
                        wi.subtasks = updated
                    after = st.progress(updated)
                    missing = st.unverified_verifiable(updated)
                    # A run is a single terminal unit of work (unlike a multi-turn
                    # session), so ANY unmet verifiable obligation at completion is
                    # under-delivery — no "advanced nothing" noise gate needed here.
                    if missing:
                        run.output_flags = {"v": 1, "outputs_missing": missing}
                    if changed:
                        await ws_manager.broadcast({
                            "type": "subtasks.progress",
                            "work_item_id": wi.id,
                            "progress": after,
                        })
            except Exception:
                logger.exception(
                    "[orchestrator] run %d: subtask verify failed", run_id
                )

        await db.commit()
        await _record_routing_outcome(db, run)  # P-0053 slice 2
        await _broadcast_run(run)
        logger.info(
            "[orchestrator] run %d succeeded via %s (%.4f USD)", run_id, run.provider, run.cost_usd
        )


# ── startup reaper (D-0021) ─────────────────────────────────────────────────────

async def reap_orphaned_runs() -> int:
    """Reconcile runs stranded by a backend restart; return how many were reaped.

    Runs execute as in-memory fire-and-forget asyncio tasks (no durable queue), so a
    crash/restart leaves any `queued`, `planning`, or `running` run with no executor — it
    would sit non-terminal forever (a `planning` run in particular keeps showing the live
    pulse when reopened). On startup we mark these `failed` with a clear reason so the
    state is honest and the user can requeue (P5: tasks are the real-work unit). Note:
    `deferred` runs are intentionally left alone — the scheduler's deferred-sweep owns
    those. Durable queueing/auto-requeue is a later managed-scale graduation (D-0021).
    """
    now = datetime.now(UTC)
    reaped = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Run).where(Run.status.in_(("running", "queued", "planning")))
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

_ACTIVE_RUN_STATUSES = ("queued", "planning", "running", "deferred")
# Statuses we must not overwrite when reconciling a crashed run (terminal + deferred,
# which is owned by the deferred sweep).
_TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled", "deferred")


async def enqueue_run(
    task_id: int, trigger: str = "manual", *, idempotency_key: str | None = None
) -> Run:
    """Create a queued Run and fire-and-forget execute_run as a background task.

    If idempotency_key is given and an active (non-terminal) run already carries
    it, return that run instead of creating a duplicate (P-0025 #2) — so a retried
    submit or a scheduler misfire-catchup can't spawn two runs for one intent.
    """
    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

        if idempotency_key is not None:
            existing = await db.execute(
                select(Run).where(
                    Run.idempotency_key == idempotency_key,
                    Run.status.in_(_ACTIVE_RUN_STATUSES),
                ).order_by(Run.id.desc()).limit(1)
            )
            dup = existing.scalar_one_or_none()
            if dup is not None:
                logger.info(
                    "[orchestrator] enqueue deduped on idempotency_key=%s → run %d",
                    idempotency_key, dup.id,
                )
                return dup

        run = Run(
            owner_id=task.owner_id,
            task_id=task_id,
            # S0 substrate: inherit the task's project/work-item so run history
            # stays project-queryable even if the task is later moved.
            project_id=task.project_id,
            work_item_id=task.work_item_id,
            trigger=trigger,
            status="queued",
            idempotency_key=idempotency_key,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    bg_task = asyncio.create_task(execute_run(run_id))
    _cancel_handles[run_id] = bg_task

    def _on_done(t: asyncio.Task) -> None:
        _cancel_handles.pop(run_id, None)
        # Surface a swallowed crash (execute_run already reconciles the row, but the
        # task's exception would otherwise vanish with no log).
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("[orchestrator] run %d background task failed: %r", run_id, exc)

    bg_task.add_done_callback(_on_done)

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
                await _record_routing_outcome(db, run)  # P-0053 slice 2
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
    run.error = redact_text(error)  # error text often embeds stderr (A6)
    run.finished_at = datetime.now(UTC)
    await db.commit()
    await _record_routing_outcome(db, run)  # P-0053 slice 2 (no-op if no decision)
    await _broadcast_run(run)


async def _record_routing_decision(db, run: Run, trace) -> None:
    """Persist a RoutingDecision from the router's trace (P-0053).

    Best-effort and never load-bearing: any failure is logged and swallowed so a
    telemetry write can't fail a run. Content-free — only candidate metadata.
    """
    if trace is None:
        return
    try:
        t = trace.to_dict()
        db.add(RoutingDecision(
            owner_id=run.owner_id,
            run_id=run.id,
            task_id=run.task_id,
            project_id=run.project_id,  # S0: content-free project correlation
            policy_version=t.get("policy_version", "rule-v1"),
            strategy=t.get("strategy", ""),
            confidential=bool(t.get("confidential")),
            degraded=bool(t.get("degraded")),
            deployment_mode=t.get("deployment_mode"),
            deferred=bool(t.get("deferred")),
            deciding_reason=(t.get("deciding_reason") or "")[:256],
            requested_candidates=t.get("requested_candidates"),
            evaluated=t.get("evaluated"),
            chosen=t.get("chosen"),
            chosen_candidates=t.get("chosen_candidates"),
            overflow_to=t.get("overflow_to"),
        ))
        await db.commit()
    except Exception:
        logger.exception("[orchestrator] run %s — failed to record routing decision", run.id)
        await db.rollback()


def _safe_duration_ms(run: Run) -> int | None:
    """Run duration in ms, tolerant of a tz-aware finished_at vs. a DB-reloaded
    tz-naive started_at (SQLite drops tzinfo) — which would make `Run.duration_ms`
    raise on the mixed subtraction at finalization."""
    start, finish = run.started_at, run.finished_at
    if not start or not finish:
        return None
    try:
        if (start.tzinfo is None) != (finish.tzinfo is None):
            start = start.replace(tzinfo=None)
            finish = finish.replace(tzinfo=None)
        return int((finish - start).total_seconds() * 1000)
    except Exception:
        return None


def _derive_routing_outcome(run: Run, chosen: str | None) -> dict:
    """Pure: realized routing outcome from a finalized run (P-0053 slice 2).

    `failover_used` is True when the primary choice didn't produce the result —
    either an explicit overflow or the executed provider differs from `chosen`.
    Kept pure (no DB) so it is unit-testable without fixtures.
    """
    executed = run.provider
    attempts = run.attempts or []
    failover_used = bool(
        getattr(run, "overflow_used", False)
        or (executed and chosen and executed != chosen)
    )
    return {
        "outcome_status": run.status,
        "executed_provider": executed,
        "executed_model": run.model,
        "failover_used": failover_used,
        "attempt_count": len(attempts),
        "outcome_cost_usd": run.cost_usd,
        "outcome_duration_ms": _safe_duration_ms(run),
    }


async def _record_routing_outcome(db, run: Run) -> None:
    """Link the realized outcome back onto this run's RoutingDecision (P-0053 slice 2).

    Best-effort and never load-bearing — a telemetry write must not fail a run. Safely
    no-ops when no decision was recorded (e.g. a pre-routing failure like task-not-found).
    """
    try:
        result = await db.execute(
            select(RoutingDecision)
            .where(RoutingDecision.run_id == run.id)
            .order_by(RoutingDecision.id.desc())
            .limit(1)
        )
        decision = result.scalar_one_or_none()
        if decision is None:
            return
        for key, val in _derive_routing_outcome(run, decision.chosen).items():
            setattr(decision, key, val)
        decision.outcome_at = datetime.now(UTC)
        await db.commit()
    except Exception:
        logger.exception("[orchestrator] run %s — failed to record routing outcome", run.id)
        await db.rollback()


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
    # Secrets wall (D-0058 A6): the durable event log must never carry a key an
    # agent echoed into stdout/stderr. The live WS stream and the deliverable
    # itself are not rewritten — the wall is the durable record.
    ev = RunEvent(
        run_id=run.id,
        seq=seq,
        kind=kind.value,
        phase=phase or message[:64],
        message=redact_text(message) if message else message,
        data=redact_json(safe_data) if safe_data else safe_data,
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
