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
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

from app.providers.base import (
    EventKind,
    ExecEvent,
    ExecResult,
    Executor,
    Usage,
)
from app.providers.registry import ProviderDef, ProviderInstance
from app.config import get_settings
from app import sandbox

logger = logging.getLogger(__name__)
_settings = get_settings()

# ── Rate-limit patterns ───────────────────────────────────────────────────────
# Each CLI words its limit differently; match broadly and parse reset timestamp.
_RATE_LIMIT_PATTERNS = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"limit.?reached", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"quota.?exceeded", re.IGNORECASE),
    re.compile(r"usage.?limit", re.IGNORECASE),
    re.compile(r"tokens per", re.IGNORECASE),
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


def _parse_reset_at(text: str) -> Optional[datetime]:
    """Try to extract a reset timestamp from rate-limit error text."""
    for pat in _RESET_PATTERNS:
        m = pat.search(text)
        if m:
            val = m.group(1).strip()
            # Seconds-based
            if val.isdigit():
                return datetime.now(timezone.utc) + timedelta(seconds=int(val))
            # ISO-ish timestamp
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return datetime.now(timezone.utc) + timedelta(seconds=_DEFAULT_COOLDOWN_SECONDS)


# ── Tolerant parser (§5.4) ────────────────────────────────────────────────────

def parse_line(line: str, accumulated_text: list[str]) -> Optional[ExecEvent]:
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
            return ExecEvent(kind=EventKind.subagent, message=obj.get("message", "subagent"), data=obj)

        # Claude stream-json v2: text delta {"type":"text","data":"..."}
        if obj_type == "text" and "data" in obj:
            t = str(obj["data"])
            if t:
                accumulated_text.append(t)
                return ExecEvent(kind=EventKind.token, text=t)

        # Claude end-of-stream signal {"type":"end","stopReason":"..."}
        # Return as a log event with full data so the executor loop can detect it.
        if obj_type == "end":
            return ExecEvent(kind=EventKind.log, message=f"[end] {obj.get('stopReason', '')}", data=obj)

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
                tokens_in=usage_raw.get("input_tokens", 0) + usage_raw.get("cached_input_tokens", 0),
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


# ── CLI command builders ──────────────────────────────────────────────────────
# Verified against --help 2026-06-01:
#   claude 2.1.159: -p/--print, --verbose, --output-format stream-json,
#                   --include-partial-messages, --allowedTools,
#                   --dangerously-skip-permissions  NO --max-turns
#   grok 0.2.14:    -p/--single, --output-format streaming-json,
#                   --always-approve, --max-turns N, --no-plan
#   agy 1.0.3:      -p/--print, --dangerously-skip-permissions
#                   NO --output-format, NO --no-plan (use prompt suffix)

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
    model: Optional[str] = None,
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
        if max_rounds:
            cmd += ["--max-turns", str(max_rounds)]

    elif binary == "agy":
        # Verified: agy 1.0.3 — no --output-format or --no-plan flag; plain text via -p.
        # No --model flag — agy/antigravity picks its own model; override is ignored.
        # stdin MUST be /dev/null: agy waits on stdin without it and emits nothing.
        cmd = ["agy", "-p", headless_prompt]
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

    def __init__(self, provider_def: ProviderDef, instance: Optional["ProviderInstance"] = None) -> None:
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

    async def run_stream(
        self,
        prompt: str,
        *,
        workdir: str,
        tools_enabled: bool = True,
        max_rounds: int = 10,
        budget_usd: float = 1.0,
        extra: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[ExecEvent]:
        binary = self._def.cli_binary
        if not binary:
            yield ExecEvent(kind=EventKind.error, message=f"No CLI binary configured for {self.name}")
            return

        cmd = _build_cmd(binary, prompt, tools_enabled=tools_enabled, max_rounds=max_rounds,
                         budget_usd=budget_usd, model=self._model)
        # Privilege drop: launch the CLI as the low-priv `sandbox` user via the
        # setuid helper so it cannot read /app or control-plane /data (P-0022/D-0020).
        # No-op outside the container, where the helper is absent.
        cmd = sandbox.wrap(cmd)
        timeout = _settings.run_timeout_seconds

        yield ExecEvent(kind=EventKind.log, message=f"[{self.name}] spawning: {' '.join(cmd[:4])}…")
        yield ExecEvent(kind=EventKind.phase, phase="running")

        accumulated_text: list[str] = []
        stderr_buf: list[str] = []
        proc: Optional[asyncio.subprocess.Process] = None

        try:
            # All plan-CLIs are headless automation — none need stdin. DEVNULL prevents
            # any binary from blocking on "Reading additional input from stdin…".
            stdin_mode = asyncio.subprocess.DEVNULL
            # Point this account's CLI at its own config dir so multiple
            # subscriptions of the same provider keep independent auth (Phase B).
            env = None
            if self._instance and self._instance.cli_config_dir and self._instance.cli_config_env:
                import os
                env = os.environ.copy()
                env[self._instance.cli_config_env] = self._instance.cli_config_dir
                yield ExecEvent(
                    kind=EventKind.log,
                    message=f"[{self.name}] {self._instance.cli_config_env}={self._instance.cli_config_dir}",
                )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_mode,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )

            assert proc.stdout is not None
            assert proc.stderr is not None

            # Merge stdout events and stderr log events through a single queue so
            # both stream to the UI in real time without blocking each other.
            event_q: asyncio.Queue[Optional[ExecEvent]] = asyncio.Queue()

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
                        # Grok/Claude end-of-stream signal
                        if (item.kind == EventKind.log
                                and isinstance(item.data, dict)
                                and item.data.get("type") == "end"):
                            break
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
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

            # Check full stderr for rate-limit signals (already populated by _stderr_worker)
            stderr_all = "\n".join(stderr_buf)
            if _is_rate_limit(stderr_all):
                reset_at = _parse_reset_at(stderr_all)
                yield ExecEvent(
                    kind=EventKind.error,
                    message=f"rate_limit_reached: {stderr_all[:300]}",
                    data={"rate_limit": True, "reset_at": reset_at.isoformat() if reset_at else None},
                )
                return

            # Fallback synthesis: only when no explicit result event was produced.
            # Covers: agy plain text, grok type:"end" format (text assembled from deltas).
            # NOT used for claude (which emits type:"result" with the full text).
            if not had_terminal_result and accumulated_text:
                text = "".join(accumulated_text)
                usage = Usage()
                result = ExecResult(text=text, usage=usage, provider=self.name, model=binary)
                yield ExecEvent(
                    kind=EventKind.result,
                    message=f"[{self.name}] complete (plain-text fallback)",
                    data={"result": result, "usage": usage.__dict__},
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
            if proc and proc.returncode is None:
                proc.kill()
