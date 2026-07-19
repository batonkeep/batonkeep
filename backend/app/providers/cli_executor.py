"""
providers/cli_executor.py — Official agent CLI subprocess executor (§5, P4).

Drives claude/grok/agy via their official binaries. The no-token rule:
  - ONLY spawn the official binary against the user's own config dir.
  - NEVER read, store, forward, or reuse an OAuth token.
  - DEPLOYMENT_MODE=managed disables this entire tier at registry level.

CLI contracts (verified against --help 2026-06-01):
  claude 2.1.159: claude -p "<P>" --verbose --output-format stream-json --include-partial-messages
                  --allowedTools <tools>  (no --max-turns; budget via --max-budget-usd)
                  auto-approve: --dangerously-skip-permissions
  grok 0.2.14:    grok -p "<P>" --output-format streaming-json --always-approve --max-turns N
  agy 1.0.3:      agy -p "<P>" --dangerously-skip-permissions  (no --output-format; plain text)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from app import sandbox
from app.config import get_settings
from app.providers.base import (
    EventKind,
    ExecEvent,
    ExecResult,
    Executor,
    Usage,
)
from app.providers.registry import ProviderDef, ProviderInstance

logger = logging.getLogger(__name__)


def _terminate_sandbox_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Best-effort kill of a still-running CLI subprocess (P-0057/D-0051).

    The CLI runs as the low-priv `sandbox` user, so `batond` cannot signal it
    cross-user — a bare ``proc.kill()`` raises ``EPERM`` ("[Errno 1] Operation not
    permitted"). On an interrupt that EPERM would surface inside the teardown
    ``finally`` and *mask* the ``CancelledError``, turning a clean cancel into a
    failed turn. Reap the whole process group through the setuid helper (pgid == pid
    via ``start_new_session``), then fall back to direct signals for the
    un-split/local-dev case. Swallows the expected errors so teardown never raises.
    """
    if proc is None or proc.returncode is not None:
        return
    sandbox.reap(proc.pid)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
_settings = get_settings()

# ── Rate-limit patterns ───────────────────────────────────────────────────────
# Each CLI words its limit differently; match broadly and parse reset timestamp.
# D-0058 A4: for codex/grok these regexes are the FALLBACK — both lanes now emit
# typed failure signals on stdout (codex `turn.failed`/`error` events; grok
# `stopReason:"rate_limit"`, which its source sets only for an actual HTTP 429)
# and the parser/stream loop classifies those first. claude/agy still ride the
# stderr regex. Wordings below are auditable in the OSS sources (2026-07-16
# review): codex "You've hit your usage limit…", "Quota exceeded…", workspace
# "…out of credits…"; grok "You've hit the rate limit for your plan…".
_RATE_LIMIT_PATTERNS = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"limit.?reached", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"quota.?exceeded", re.IGNORECASE),
    re.compile(r"usage.?limit", re.IGNORECASE),
    re.compile(r"tokens per", re.IGNORECASE),
    re.compile(r"out of credits", re.IGNORECASE),  # codex workspace credit depletion
]

# Try to extract a reset time from the error message
_RESET_PATTERNS = [
    re.compile(r"resets?\s+(?:at|in|after)\s+([\d\-T:+Z ]+)", re.IGNORECASE),
    re.compile(r"try again (?:at|after)\s+([\d\-T:+Z ]+)", re.IGNORECASE),
    re.compile(r"in (\d+)\s*s(?:econds?)?", re.IGNORECASE),
    re.compile(r"wait (\d+)\s*s(?:econds?)?", re.IGNORECASE),
]

_DEFAULT_COOLDOWN_SECONDS = 300  # 5 min if no timestamp found


def _is_rate_limit(text: str) -> bool:
    return any(p.search(text) for p in _RATE_LIMIT_PATTERNS)


def _parse_reset_at(text: str) -> datetime | None:
    """Try to extract a reset timestamp from rate-limit error text."""
    for pat in _RESET_PATTERNS:
        m = pat.search(text)
        if m:
            val = m.group(1).strip()
            # Seconds-based
            if val.isdigit():
                return datetime.now(UTC) + timedelta(seconds=int(val))
            # ISO-ish timestamp
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    return datetime.strptime(val, fmt).replace(tzinfo=UTC)
                except ValueError:
                    continue
    return datetime.now(UTC) + timedelta(seconds=_DEFAULT_COOLDOWN_SECONDS)


# ── Tolerant parser (§5.4) ────────────────────────────────────────────────────

def parse_line(line: str, accumulated_text: list[str]) -> ExecEvent | None:
    """
    Parse one stdout line from a CLI agent.
    Returns an ExecEvent or None if the line should be buffered as plain text.

    Must survive NDJSON, single-object JSON, and plain text.
    """
    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # Plain text line — buffer as token
        accumulated_text.append(line + "\n")
        return ExecEvent(kind=EventKind.token, text=line + "\n")

    # ── Detect final / result object ─────────────────────────────────────────
    # Claude: {"type":"result","result":"...", "cost_usd":..., "usage":{...}}
    # Generic: any object with "result" key or "cost_usd"/"total_cost_usd"
    if isinstance(obj, dict):
        obj_type = obj.get("type", "")

        # Final result event — create a proper ExecResult so the orchestrator can
        # use it directly without needing the fallback synthesis path.
        # Do NOT append to accumulated_text here (streaming deltas already have it).
        if obj_type == "result" or ("result" in obj and "cost_usd" in obj):
            result_text = obj.get("result", "") or ""
            # If the CLI's result field is empty, fall back to what was streamed.
            final_text = result_text if result_text else "".join(accumulated_text)
            usage_raw = obj.get("usage", {})
            usage = Usage(
                tokens_in=usage_raw.get("input_tokens", 0) if isinstance(usage_raw, dict) else 0,
                tokens_out=usage_raw.get("output_tokens", 0) if isinstance(usage_raw, dict) else 0,
                cost_usd=float(obj.get("cost_usd", obj.get("total_cost_usd", 0.0))),
            )
            exec_result = ExecResult(text=final_text, usage=usage, provider="cli", model="cli")
            return ExecEvent(
                kind=EventKind.result,
                message="CLI result",
                data={"result": exec_result, "usage": usage.__dict__, "raw": obj},
                text=final_text,
            )

        # Token/text delta events (various CLI formats)
        if obj_type in ("stream_event", "content_block_delta"):
            # Claude stream-json delta
            delta_text = (
                obj.get("event", {}).get("delta", {}).get("text", "")
                or obj.get("delta", {}).get("text", "")
            )
            if delta_text:
                accumulated_text.append(delta_text)
                return ExecEvent(kind=EventKind.token, text=delta_text)

        if obj_type == "assistant":
            # Claude sends the complete assembled message after streaming all deltas.
            # Suppress it as a token (it's a duplicate of what was already streamed)
            # and do NOT accumulate — deltas in accumulated_text are already correct.
            return ExecEvent(kind=EventKind.log, message="[assistant block]", data=obj)

        if obj_type == "tool_use":
            return ExecEvent(kind=EventKind.tool, message=obj.get("name", "tool_use"), data=obj)

        if obj_type == "subagent":
            return ExecEvent(
                kind=EventKind.subagent, message=obj.get("message", "subagent"), data=obj
            )

        # Claude stream-json v2: text delta {"type":"text","data":"..."}
        if obj_type == "text" and "data" in obj:
            t = str(obj["data"])
            if t:
                accumulated_text.append(t)
                return ExecEvent(kind=EventKind.token, text=t)

        # Grok turn-cap abort {"type":"max_turns_reached"}. Surface as a log with data
        # intact so the executor loop can flag the run as truncated (not completed).
        if obj_type == "max_turns_reached":
            return ExecEvent(kind=EventKind.log, message="[max_turns_reached]", data=obj)

        # Claude end-of-stream signal {"type":"end","stopReason":"..."}
        # Return as a log event with full data so the executor loop can detect it.
        if obj_type == "end":
            return ExecEvent(
                kind=EventKind.log, message=f"[end] {obj.get('stopReason', '')}", data=obj
            )

        # ── Typed failure events (D-0058 A4) ─────────────────────────────────
        # codex exec --json: {"type":"turn.failed","error":{"message":…}} and
        # {"type":"error","message":…} ("unrecoverable error emitted directly by
        # the event stream"); grok's headless emitter uses the same
        # {"type":"error","message":…} shape. These are the typed signal —
        # classify rate limits here so the stderr regex in run_stream stays a
        # fallback for the structured lanes. codex usage-limit messages carry a
        # "…try again at <time>." suffix that _parse_reset_at picks up.
        if obj_type in ("turn.failed", "error"):
            err_obj = obj.get("error")
            msg = str(err_obj.get("message") or "") if isinstance(err_obj, dict) else ""
            if not msg:
                msg = str(obj.get("message") or "") or line[:300]
            if _is_rate_limit(msg):
                return ExecEvent(
                    kind=EventKind.error,
                    message=f"rate_limit_reached: {msg[:300]}",
                    data={
                        "rate_limit": True,
                        "reset_at": _parse_reset_at(msg).isoformat(),
                        "raw": obj,
                    },
                )
            return ExecEvent(kind=EventKind.error, message=msg[:300], data={"raw": obj})

        # Grok streaming-json delta (text field at top level)
        if "text" in obj and obj_type not in ("result",):
            t = obj["text"]
            accumulated_text.append(t)
            return ExecEvent(kind=EventKind.token, text=t)

        # ── Codex exec --json JSONL format (codex-cli 0.136+) ────────────────
        # Events: thread.started, turn.started, item.started, item.completed, turn.completed

        if obj_type == "turn.completed":
            # Terminal event with full usage. Build ExecResult from the last agent_message
            # (accumulated_text is replaced on each agent_message, so only the final remains).
            usage_raw = obj.get("usage", {})
            usage = Usage(
                tokens_in=(
                    usage_raw.get("input_tokens", 0) + usage_raw.get("cached_input_tokens", 0)
                ),
                tokens_out=usage_raw.get("output_tokens", 0),
                cost_usd=0.0,  # subscription plan — no per-token charge
            )
            final_text = "".join(accumulated_text)
            exec_result = ExecResult(text=final_text, usage=usage, provider="cli", model="cli")
            return ExecEvent(
                kind=EventKind.result,
                message="codex complete",
                data={"result": exec_result, "usage": usage.__dict__, "raw": obj},
                text=final_text,
            )

        if obj_type == "item.completed" and isinstance(obj.get("item"), dict):
            item = obj["item"]
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "") or ""
                if text:
                    # Replace (not append): only the LAST agent_message is the final report;
                    # earlier ones are intermediate reasoning/planning text.
                    accumulated_text.clear()
                    accumulated_text.append(text)
                    return ExecEvent(kind=EventKind.token, text=text)
            elif item_type == "command_execution" and item.get("status") == "completed":
                cmd = item.get("command", "")
                out = item.get("aggregated_output", "")
                return ExecEvent(
                    kind=EventKind.tool,
                    message=cmd[:120],
                    data={"command": cmd, "output": out[:500], "exit_code": item.get("exit_code")},
                )

        if obj_type == "item.started" and isinstance(obj.get("item"), dict):
            item = obj["item"]
            if item.get("type") == "command_execution":
                return ExecEvent(kind=EventKind.tool,
                                 message=f"$ {item.get('command', '')[:100]}",
                                 data={"status": "starting"})

        if obj_type in ("thread.started", "turn.started"):
            return ExecEvent(kind=EventKind.log, message=f"[{obj_type}]", data=obj)

        # Unrecognised object — log it
        return ExecEvent(kind=EventKind.log, message=line[:200], data=obj)

    # Non-dict JSON (array, etc.) — log
    return ExecEvent(kind=EventKind.log, message=line[:200])


# ── Plain-text narration stripping (agy) ───────────────────────────────────────
# Structured-stream CLIs (claude/grok/codex) separate planning/reasoning from the
# final answer, so the parser can keep only the deliverable. `agy -p` emits plain
# text with no such structure: the agent's step-by-step narration ("I will list…",
# "I will run…") is interleaved ahead of the actual report and, without this,
# every line is buffered into the result. We strip a *leading run* of first-person
# planning narration when it sits in front of the real Markdown report.

# First-person planning openers agy uses to narrate each tool action.
_NARRATION_RE = re.compile(
    r"^\s*(?:I['’]?\s*(?:will|ll|am going to|m going to|need to|should|have|"
    r"can|now|next)\b|I\s+will\b|Let me\b|Let['’]?s\b|First,?\s+I\b|"
    r"Next,?\s+I\b|Now\s+I\b|Then\s+I\b)",
    re.IGNORECASE,
)
# A line that begins the structured report proper: heading, table row, fenced
# block, blockquote, or list item. The deliverable starts at the first of these.
_REPORT_START_RE = re.compile(r"^\s*(?:#{1,6}\s|\||```|>\s|[-*+]\s|\d+\.\s)")


def strip_agent_narration(text: str) -> str:
    """Drop a leading block of agent planning-narration from plain-text output.

    Conservative: only strips when the text contains a real Markdown report
    further down AND every non-blank line before it is first-person planning
    narration. If the preamble holds any other prose, or there is no structured
    report to fall back to, the text is returned unchanged (better a noisy report
    than a blanked one — pure-narration runs, where the report was written only to
    a file, are recovered by the orchestrator's agent-file scan instead).
    """
    lines = text.split("\n")
    # Index of the first line that looks like the start of the real report.
    start = next(
        (i for i, ln in enumerate(lines) if _REPORT_START_RE.match(ln)),
        None,
    )
    if start is None or start == 0:
        return text  # nothing structured to keep, or report already leads
    # Every non-blank line before the report must be planning narration.
    preamble = [ln for ln in lines[:start] if ln.strip()]
    if not preamble or not all(_NARRATION_RE.match(ln) for ln in preamble):
        return text
    return "\n".join(lines[start:]).lstrip("\n")


# ── CLI command builders ──────────────────────────────────────────────────────
# Verified against --help (agy re-verified 2026-07-16, others 2026-06-01):
#   claude 2.1.159: -p/--print, --verbose, --output-format stream-json,
#                   --include-partial-messages, --allowedTools,
#                   --dangerously-skip-permissions  NO --max-turns
#   grok 0.2.14:    -p/--single, --output-format streaming-json,
#                   --always-approve, --max-turns N, --no-plan
#   agy 1.1.2:      -p/--print, --dangerously-skip-permissions, --model,
#                   --print-timeout (default 5m!)  NO --output-format,
#                   NO --no-plan (use prompt suffix)
# These CLIs auto-update in the field (the image installs latest at build), so a
# flag drift here fails loudly at spawn — the D-0058 A2 version probe will record
# the executing version per run; until then, re-verify on upstream releases (the
# weekly changelog sweep in the ops loop).

# ── CLI version probe (provenance stamps) ─────────────────────────────────────
# The CLIs auto-update in the field, so the executing version must land in the
# durable record (ContextReceipt.cli_version) or provider-fit comparisons can't
# tell a model regression from a CLI regression. One `<binary> --version` probe
# per binary per process, cached — including failures (None), so a missing
# binary isn't probed on every run.
_CLI_VERSION_CACHE: dict[str, str | None] = {}


def probe_cli_version(binary: str) -> str | None:
    """Cached `<binary> --version` (first line, ≤64 chars). None if unprobeable."""
    if binary in _CLI_VERSION_CACHE:
        return _CLI_VERSION_CACHE[binary]
    version: str | None = None
    try:
        import subprocess
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10,
        )
        first = (out.stdout or out.stderr or "").strip().splitlines()
        version = first[0].strip()[:64] if first else None
    except (OSError, subprocess.SubprocessError):
        version = None
    _CLI_VERSION_CACHE[binary] = version
    return version


# Appended to every CLI prompt so agents don't stall in planning/interactive mode.
_HEADLESS_SUFFIX = (
    "\n\n---\n"
    "This is a headless, non-interactive run: do not use planning mode, "
    "do not generate any implementation plans or walkthroughs, "
    "do not offer next actions, ask questions, or wait for user input at the end. "
    "Produce your final output and exit immediately."
)


def _build_cmd(
    binary: str, prompt: str, *, tools_enabled: bool, max_rounds: int, budget_usd: float = 0.0,
    model: str | None = None,
) -> list[str]:
    auto_approve = _settings.autonomous_tools and tools_enabled
    headless_prompt = prompt + _HEADLESS_SUFFIX

    if binary == "claude":
        # Verified: claude 2.1.159
        cmd = ["claude", "-p", headless_prompt, "--verbose", "--output-format", "stream-json",
               "--include-partial-messages"]
        if model:
            cmd += ["--model", model]
        if auto_approve:
            # --dangerously-skip-permissions bypasses all permission prompts in automation
            cmd += ["--dangerously-skip-permissions"]
            cmd += ["--allowedTools", "WebSearch,Read,Write"]
        if budget_usd > 0:
            cmd += ["--max-budget-usd", str(budget_usd)]
        # Note: no --max-turns on claude; turns are controlled implicitly by budget/prompt

    elif binary == "grok":
        # Verified: grok 0.2.14 — --no-plan suppresses planning mode stall
        cmd = ["grok", "-p", headless_prompt, "--output-format", "streaming-json", "--no-plan"]
        if model:
            cmd += ["--model", model]
        if auto_approve:
            cmd += ["--always-approve"]
        # No --max-turns: grok counts each reasoning/tool step as a turn, so a low cap
        # (the orchestrator's max_rounds=10) gets exhausted mid-research and grok aborts
        # with max_turns_reached / stopReason:"Cancelled" before writing the report. Match
        # claude (no turn cap) and let run_timeout_seconds bound any runaway run instead.

    elif binary == "agy":
        # Verified: agy 1.1.2 (D-0058 item 9) — still no --output-format (plain text
        # via -p; narration stripping below stays). Two stale 1.0.3 assumptions fixed:
        #   • --model exists now (1.1.2+ print mode hard-fails on an unresolvable
        #     model instead of silently downgrading — fail-loud is what we want);
        #   • --print-timeout defaults to 5m and was silently truncating longer runs —
        #     align it to the orchestrator's wall-clock bound, which stays the owner
        #     of run cancellation.
        # stdin stays /dev/null: required <1.1.1 (agy read stdin and hung), harmless
        # after. 1.1.1 also fixed print-mode false-success (server error → exit 0 +
        # empty output), so non-zero exit is trustworthy again from that version on.
        cmd = ["agy", "-p", headless_prompt,
               "--print-timeout", f"{_settings.run_timeout_seconds}s"]
        if model:
            cmd += ["--model", model]
        if auto_approve:
            cmd += ["--dangerously-skip-permissions"]

    elif binary == "codex":
        # Verified: codex-cli 0.136.0
        # `exec` subcommand runs non-interactively; --json gives JSONL event stream.
        # --ephemeral: no session files persisted. --skip-git-repo-check: runs anywhere.
        # stdin MUST be /dev/null: codex reads stdin even with a prompt arg; it would
        # otherwise print "Reading additional input from stdin..." and block.
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral"]
        if model:
            cmd += ["--model", model]
        if auto_approve:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        cmd += [headless_prompt]

    else:
        raise ValueError(f"Unknown CLI binary: {binary}")

    return cmd


# ── Executor ──────────────────────────────────────────────────────────────────

class CLIExecutor(Executor):
    """
    Spawns official CLI agent binaries as async subprocesses.

    Hard rule: never reads, stores, or forwards OAuth tokens.
    The binary is given its own config dir (via HOME on the volume) and
    manages its own auth entirely.
    """

    def __init__(self, provider_def: ProviderDef, instance: ProviderInstance | None = None) -> None:
        self._def = provider_def
        self._instance = instance
        # name is the instance id so run records / cooldown key per-account.
        self.name = instance.id if instance else provider_def.name
        self.tier = "agent"
        # Model is NOT overridden here: each plan-CLI owns its own model selection
        # via its config dir (set with the CLI's interactive `/model` picker through
        # the in-UI console). Forcing --model would create a second, competing
        # source of truth that silently shadows the user's choice. Headless runs
        # inherit whatever the CLI is configured to use.
        self._model = None

    @property
    def kind(self) -> str:
        return "cli"

    def is_healthy(self) -> bool:
        """Check if the binary is available on PATH."""
        import shutil
        return bool(self._def.cli_binary and shutil.which(self._def.cli_binary))

    def cli_version(self) -> str | None:
        """The executing binary's probed version (provenance stamps)."""
        return probe_cli_version(self._def.cli_binary) if self._def.cli_binary else None

    async def run_stream(
        self,
        prompt: str,
        *,
        workdir: str,
        tools_enabled: bool = True,
        max_rounds: int = 10,
        budget_usd: float = 1.0,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        binary = self._def.cli_binary
        if not binary:
            yield ExecEvent(
                kind=EventKind.error, message=f"No CLI binary configured for {self.name}"
            )
            return

        cmd = _build_cmd(binary, prompt, tools_enabled=tools_enabled, max_rounds=max_rounds,
                         budget_usd=budget_usd, model=self._model)
        # Privilege drop: launch the CLI as the low-priv `sandbox` user via the
        # setuid helper so it cannot read /app or control-plane /data (P-0022/D-0020).
        # No-op outside the container, where the helper is absent — but fails CLOSED
        # under REQUIRE_SANDBOX rather than running the CLI as the control-plane user.
        try:
            cmd = sandbox.wrap(cmd)
        except sandbox.SandboxUnavailableError as exc:
            yield ExecEvent(kind=EventKind.error, message=f"[{self.name}] {exc}")
            return
        timeout = _settings.run_timeout_seconds

        yield ExecEvent(kind=EventKind.log, message=f"[{self.name}] spawning: {' '.join(cmd[:4])}…")
        yield ExecEvent(kind=EventKind.phase, phase="running")

        accumulated_text: list[str] = []
        stderr_buf: list[str] = []
        proc: asyncio.subprocess.Process | None = None

        try:
            # All plan-CLIs are headless automation — none need stdin. DEVNULL prevents
            # any binary from blocking on "Reading additional input from stdin…".
            stdin_mode = asyncio.subprocess.DEVNULL
            # Point this account's CLI at its own config dir so multiple
            # subscriptions of the same provider keep independent auth (Phase B).
            env = None
            trust = sandbox.git_trust_env(workdir)
            if trust:
                env = os.environ.copy()
                env.update(trust)
            if self._instance and self._instance.cli_config_dir and self._instance.cli_config_env:
                env = env if env is not None else os.environ.copy()
                env[self._instance.cli_config_env] = self._instance.cli_config_dir
                yield ExecEvent(
                    kind=EventKind.log,
                    message=(
                        f"[{self.name}] "
                        f"{self._instance.cli_config_env}={self._instance.cli_config_dir}"
                    ),
                )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_mode,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
                # Own process group (pgid == pid) so an interrupt can reap the whole
                # agent tree via the setuid helper — batond cannot signal the
                # `sandbox`-uid process cross-user (P-0057/D-0051).
                start_new_session=True,
            )

            assert proc.stdout is not None
            assert proc.stderr is not None

            # Merge stdout events and stderr log events through a single queue so
            # both stream to the UI in real time without blocking each other.
            event_q: asyncio.Queue[ExecEvent | None] = asyncio.Queue()

            async def _stdout_worker() -> None:
                """Parse stdout lines into events; puts None sentinel when done."""
                try:
                    async for raw_line in proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace")
                        ev = parse_line(line, accumulated_text)
                        if ev is not None:
                            await event_q.put(ev)
                            if ev.is_terminal():
                                return
                except Exception as exc:
                    await event_q.put(ExecEvent(kind=EventKind.error, message=str(exc)))
                finally:
                    await event_q.put(None)  # sentinel: stdout stream done

            async def _stderr_worker() -> None:
                """Stream each stderr line as a live log event for real-time feedback."""
                async for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        stderr_buf.append(line)
                        await event_q.put(ExecEvent(
                            kind=EventKind.log,
                            message=f"[{self.name}] {line}",
                        ))

            t_out = asyncio.create_task(_stdout_worker())
            t_err = asyncio.create_task(_stderr_worker())

            # Track whether we yielded a proper result event (with ExecResult in data).
            # If so, the fallback synthesis below must NOT fire.
            had_terminal_result = False
            # Set when the agent aborted before producing a deliverable (grok turn-cap
            # or a non-EndTurn stop reason). The fallback below must report a failure,
            # not "complete", even if partial reasoning text was accumulated.
            truncated_reason: str | None = None
            # D-0058 A4: grok's headless stream maps an actual HTTP 429 (and only
            # a 429 — its source contract) to stopReason:"rate_limit" on the end
            # event. Typed signal; the stderr regex below becomes the fallback.
            rate_limited_stop = False

            try:
                async with asyncio.timeout(timeout):
                    while True:
                        item = await event_q.get()
                        if item is None:  # stdout EOF → exit and run fallback below
                            break
                        yield item
                        if item.is_terminal():
                            if item.kind == EventKind.result:
                                had_terminal_result = True
                            break
                        if item.kind == EventKind.log and isinstance(item.data, dict):
                            itype = item.data.get("type")
                            if itype == "max_turns_reached":
                                truncated_reason = "max turns reached"
                                continue  # the terminating 'end' event still follows
                            # Grok/Claude end-of-stream signal
                            if itype == "end":
                                stop = str(item.data.get("stopReason", ""))
                                if stop.lower() == "rate_limit":
                                    rate_limited_stop = True
                                    break
                                # Anything other than a normal completion means the run
                                # was cut short (e.g. grok emits stopReason:"Cancelled"
                                # after max_turns_reached).
                                if stop and stop.lower() not in ("endturn", "end_turn", "stop"):
                                    truncated_reason = truncated_reason or f"stopReason={stop}"
                                break
            except TimeoutError:
                _terminate_sandbox_proc(proc)
                yield ExecEvent(
                    kind=EventKind.error,
                    message=f"[{self.name}] timeout after {timeout}s",
                )
                return
            finally:
                t_out.cancel()
                t_err.cancel()
                await asyncio.gather(t_out, t_err, return_exceptions=True)

            await proc.wait()

            stderr_all = "\n".join(stderr_buf)

            # Typed grok rate-limit stop (A4): the end event said 429 explicitly.
            # No reset info rides the stream (data is null for rate_limit stops);
            # stderr may still word a reset time, else the default cooldown applies.
            if rate_limited_stop:
                reset_at = _parse_reset_at(stderr_all)
                yield ExecEvent(
                    kind=EventKind.error,
                    message="rate_limit_reached: provider stream reported "
                            "stopReason=rate_limit",
                    data={
                        "rate_limit": True,
                        "reset_at": reset_at.isoformat() if reset_at else None,
                    },
                )
                return

            # Fallback (claude/agy — and any lane whose typed signal was absent):
            # regex over the full stderr, already populated by _stderr_worker.
            if _is_rate_limit(stderr_all):
                reset_at = _parse_reset_at(stderr_all)
                yield ExecEvent(
                    kind=EventKind.error,
                    message=f"rate_limit_reached: {stderr_all[:300]}",
                    data={
                        "rate_limit": True,
                        "reset_at": reset_at.isoformat() if reset_at else None,
                    },
                )
                return

            # Truncated/cancelled run: the agent aborted before finishing (e.g. grok
            # exhausted its turns and emitted stopReason:"Cancelled"). Any accumulated
            # text is partial reasoning, not the deliverable — report an honest failure
            # so the orchestrator can fail over instead of storing an empty result.
            if not had_terminal_result and truncated_reason:
                yield ExecEvent(
                    kind=EventKind.error,
                    message=(
                        f"[{self.name}] run did not complete ({truncated_reason}); "
                        "no final output produced"
                    ),
                )
                return

            # Fallback synthesis: only when no explicit result event was produced.
            # Covers: agy plain text, grok type:"end" format (text assembled from deltas).
            # NOT used for claude (which emits type:"result" with the full text).
            if not had_terminal_result and accumulated_text:
                # Plain-text providers (agy) interleave planning narration ahead of
                # the report; strip a leading narration block so it doesn't become
                # the deliverable. No-op when the text already leads with the report
                # (e.g. grok deltas), so structured-stream output is untouched.
                text = strip_agent_narration("".join(accumulated_text))
                usage = Usage()
                result = ExecResult(text=text, usage=usage, provider=self.name, model=binary)
                yield ExecEvent(
                    kind=EventKind.result,
                    message=f"[{self.name}] complete (plain-text fallback)",
                    data={"result": result, "usage": usage.__dict__},
                )
            elif not had_terminal_result:
                # Stream ended with no result event AND no usable text (e.g. the agent
                # emitted only reasoning/thoughts then EndTurn, or exited non-zero).
                # Emit an explicit error so the orchestrator records an honest failure
                # instead of receiving no terminal event and silently re-running.
                tail = (stderr_all or "")[-300:]
                yield ExecEvent(
                    kind=EventKind.error,
                    message=(
                        f"[{self.name}] agent exited without output "
                        f"(returncode={proc.returncode})"
                        + (f": {tail}" if tail else "")
                    ),
                )

        except FileNotFoundError:
            yield ExecEvent(
                kind=EventKind.error,
                message=f"[{self.name}] binary not found: {binary}. Run `make auth` to install.",
            )
        except Exception as exc:
            logger.exception("[%s] unexpected error", self.name)
            yield ExecEvent(kind=EventKind.error, message=f"[{self.name}] {exc}")
        finally:
            # Teardown must never raise — a bare proc.kill() on the sandbox-uid CLI
            # would EPERM and mask an in-flight CancelledError (P-0057/D-0051).
            _terminate_sandbox_proc(proc)
