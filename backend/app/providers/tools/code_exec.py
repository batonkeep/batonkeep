"""
providers/tools/code_exec.py — run Python in the pinned exec env (P-0046 Tier A).

Because Batonkeep owns and pins the Linux exec env (the exec-env manifest,
Option C — `app/exec_env.py`), code-exec is a *consistent, reliable* capability
rather than probe-and-hope: a guaranteed toolchain the agent can rely on. A
capable code-exec + the right libraries subsumes whole tool categories
(PDF/CSV/chart/scrape), which is the actual CLI-parity lever.

Execution bounds (V1, single-tenant — P-0046):
  • runs against the **separate exec venv** (`/opt/exec-env/.venv`), falling back
    to the backend interpreter only when that venv is absent (local dev/tests);
  • the session **workdir is the cwd** and the only writable surface the agent is
    handed; the process is dropped to the low-priv `sandbox` user via
    `sandbox.wrap()` (the same vertical fence the CLI agents run behind, D-0020);
  • bounded wall-clock + captured/​capped output.

Execution policy (per session/task; default **confirmation**):
  • `off`          — code-exec is not offered and refuses if called;
  • `confirmation` — requires per-execution operator approval. Interactive sessions
    supply an `approve` callback (slice 3b) that drives the round-trip; **unattended
    tasks have no human, so code-exec stays unavailable there** under this default
    ("left on confirmation, code-exec is unavailable in unattended runs");
  • `allow-safe`   — a non-destructive, no-network heuristic subset auto-runs;
  • `auto`         — runs without prompting (operator opted in).

The real isolation boundary is the sandbox (workdir-only writable, low-priv uid),
**not** the `allow-safe` static check — that check is a convenience gate, not a
security boundary, and is documented as such.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable

from app import sandbox
from app.exec_env import load as load_exec_env

logger = logging.getLogger(__name__)

# Async approval callback an interactive session injects to drive `confirmation`:
# (code, label) -> approved?  (P-0046 slice 3b)
ApproveFn = Callable[[str, "str | None"], Awaitable[bool]]

POLICIES = ("off", "confirmation", "allow-safe", "auto")
DEFAULT_POLICY = "confirmation"
# Policies under which code-exec is *offered* to the model (listed) and runnable
# without an interactive approval channel (which arrives in slice 3b).
_RUNNABLE_POLICIES = ("allow-safe", "auto")

_TIMEOUT_S = 60.0
_MAX_OUTPUT = 64 * 1024

# allow-safe heuristic denylist — patterns that imply network or shelling out.
# NOT a security boundary (the sandbox is); a convenience gate so `allow-safe`
# auto-runs only obviously-local, non-destructive snippets.
_UNSAFE_PATTERNS = [
    r"\bimport\s+socket\b", r"\bimport\s+subprocess\b", r"\bfrom\s+subprocess\b",
    r"\bimport\s+urllib\b", r"\bimport\s+requests\b", r"\bimport\s+httpx\b",
    r"\bhttp\.client\b", r"\bos\.system\b", r"\bos\.popen\b", r"\bsocket\.",
    r"\bshutil\.rmtree\b", r"\bos\.remove\b", r"\bos\.unlink\b",
]
_UNSAFE_RE = re.compile("|".join(_UNSAFE_PATTERNS))

TOOL_SCHEMA = {
    "name": "code_exec",
    "description": (
        "Execute a Python snippet in a pinned environment (guaranteed libraries: "
        "httpx, pandas, numpy, pypdf, python-docx, openpyxl, beautifulsoup4, lxml, "
        "matplotlib). The working directory is the cwd and the only writable surface "
        "— write output files there. Returns captured stdout/stderr."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
            "label": {
                "type": "string",
                "description": "Optional short description of the snippet.",
            },
        },
        "required": ["code"],
    },
}


def policy_offers_tool(policy: str | None, human_in_loop: bool = False) -> bool:
    """Whether code-exec should be listed to the model.

    `allow-safe`/`auto` always offer it. `confirmation` offers it only when a human
    is in the loop to approve each run (interactive sessions, P-0046 slice 3b) —
    unattended tasks left on `confirmation` still don't get it."""
    policy = policy or DEFAULT_POLICY
    if policy in _RUNNABLE_POLICIES:
        return True
    return policy == "confirmation" and human_in_loop


def _python_bin() -> str:
    """The exec-venv interpreter, or the backend interpreter when it's absent
    (local dev / tests / unbuilt image)."""
    env = load_exec_env()
    if os.path.exists(env.python_bin):
        return env.python_bin
    logger.info("[code_exec] exec venv absent — falling back to %s", sys.executable)
    return sys.executable


def _is_safe(code: str) -> bool:
    return _UNSAFE_RE.search(code) is None


def _reset_umask() -> None:
    """Child-process preexec: set umask 022 so installed files get conventional
    perms (dirs/bins executable). Runs after fork, before exec — inherited across
    the sandbox-spawn exec and any subprocess the snippet launches."""
    os.umask(0o022)


async def run(
    code: str, *, workdir: str, policy: str | None = None, label: str | None = None,
    approve: ApproveFn | None = None,
) -> str:
    """Run `code` under `policy`. `approve` is an async callback
    `(code, label) -> bool` supplied by interactive sessions to drive the
    `confirmation` round-trip (P-0046 slice 3b); without it `confirmation`
    refuses (unattended runs)."""
    policy = policy or DEFAULT_POLICY
    if policy == "off":
        return "[code_exec error] code execution is disabled (policy: off)"
    if policy == "confirmation":
        if approve is None:
            # No human-in-the-loop channel (e.g. unattended task). Conservative refusal.
            return (
                "[code_exec error] code execution requires operator approval "
                "(policy: confirmation); set the execution policy to allow-safe or auto "
                "to run code in this session/task"
            )
        approved = await approve(code, label)
        if not approved:
            return "[code_exec] execution denied by operator"
        # Approved → fall through and execute this one snippet.
    elif policy == "allow-safe" and not _is_safe(code):
        return (
            "[code_exec error] snippet blocked by allow-safe policy (network/"
            "subprocess/destructive call detected); requires the auto policy"
        )
    if policy not in POLICIES:
        return f"[code_exec error] unknown execution policy: {policy}"
    # Reaching here means: auto, allow-safe (passed the check), or confirmation
    # (approved). off / denied / unknown have already returned above.

    # Fail closed: in a deployment that promises isolation (REQUIRE_SANDBOX), never
    # run agent-authored code as the control-plane user just because the spawner is
    # momentarily unavailable — that is exactly the P-0046 non-sandbox bug (an
    # `npm install` that ran as `batond`). Refuse instead.
    if sandbox.required() and not sandbox.available():
        logger.critical(
            "[code_exec] REQUIRE_SANDBOX set but sandbox spawner unavailable — "
            "refusing to run un-sandboxed"
        )
        return (
            "[code_exec error] sandbox unavailable — execution refused "
            "(isolation is mandatory in this deployment)"
        )

    python_bin = _python_bin()
    fd, script_path = tempfile.mkstemp(suffix=".py", prefix=".baton_exec_", dir=workdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        # Make the script readable by the sandbox user (mkstemp is 0600).
        os.chmod(script_path, 0o644)
        # HOME is already the workdir for this lane, so the jail is exactly the
        # snippet's own workspace (P-0072).
        cmd = sandbox.wrap([python_bin, script_path], jail=workdir)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": workdir,
            "PYTHONUNBUFFERED": "1",
            # Git run from agent code hits the same mixed-uid dubious-ownership
            # fence as the CLI lanes — trust exactly this workspace.
            **sandbox.git_trust_env(workdir),
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=workdir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                # Force a sane umask in the child (inherited through sandbox-spawn +
                # any tool the snippet runs, e.g. `npm install`). Without this a stray
                # ambient umask (e.g. 007) leaves freshly-installed binaries
                # non-executable — node_modules/.bin/vite, esbuild → EACCES on build.
                preexec_fn=_reset_umask,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
        except TimeoutError:
            return f"[code_exec error] execution timed out after {int(_TIMEOUT_S)}s"
        except OSError as exc:
            return f"[code_exec error] failed to launch interpreter: {exc}"
        text = out.decode("utf-8", "replace")
        if len(text) > _MAX_OUTPUT:
            text = text[:_MAX_OUTPUT] + "\n[code_exec] output truncated]"
        rc = proc.returncode
        prefix = "[code_exec]" if rc == 0 else f"[code_exec exit {rc}]"
        return f"{prefix}\n{text}" if text.strip() else f"{prefix} (no output)"
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
