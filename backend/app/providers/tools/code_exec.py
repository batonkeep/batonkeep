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
  • `confirmation` — requires per-execution operator approval. The interactive
    approval round-trip is a later slice (3b); until then code-exec is simply not
    offered under this policy (the conservative default), matching the proposal:
    "left on confirmation, code-exec is unavailable in unattended runs";
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

from app import sandbox
from app.exec_env import load as load_exec_env

logger = logging.getLogger(__name__)

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


def policy_offers_tool(policy: str | None) -> bool:
    """Whether code-exec should be listed to the model under `policy` (3a)."""
    return (policy or DEFAULT_POLICY) in _RUNNABLE_POLICIES


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


async def run(
    code: str, *, workdir: str, policy: str | None = None, label: str | None = None
) -> str:
    policy = policy or DEFAULT_POLICY
    if policy == "off":
        return "[code_exec error] code execution is disabled (policy: off)"
    if policy == "confirmation":
        # No interactive approval channel yet (slice 3b). Conservative refusal.
        return (
            "[code_exec error] code execution requires operator approval "
            "(policy: confirmation); set the execution policy to allow-safe or auto "
            "to run code in this session/task"
        )
    if policy == "allow-safe" and not _is_safe(code):
        return (
            "[code_exec error] snippet blocked by allow-safe policy (network/"
            "subprocess/destructive call detected); requires the auto policy"
        )
    if policy not in _RUNNABLE_POLICIES:
        return f"[code_exec error] unknown execution policy: {policy}"

    python_bin = _python_bin()
    fd, script_path = tempfile.mkstemp(suffix=".py", prefix=".baton_exec_", dir=workdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        # Make the script readable by the sandbox user (mkstemp is 0600).
        os.chmod(script_path, 0o644)
        cmd = sandbox.wrap([python_bin, script_path])
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": workdir,
            "PYTHONUNBUFFERED": "1",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=workdir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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
