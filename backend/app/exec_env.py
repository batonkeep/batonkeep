"""
app/exec_env.py — read the exec-env manifest and guard the built image against it.

The manifest (`exec-env.toml`) is the single source of truth for the guaranteed
code-exec toolchain (P-0046, Option C). This module is the runtime consumer:

  • `render_capabilities()` — the toolchain blurb the agent system prompt embeds
    so the model knows what it can rely on without probing.
  • `verify(strict=…)` — the **drift-guard**: asserts the built exec venv actually
    contains every `[python].guaranteed` library (importable), mirroring the
    alembic schema drift-guard (D-0021). Called at boot; a mismatch means the
    image diverged from the manifest.

The exec venv (`[meta].venv_dir`) is built separately from the backend's own
`/app/.venv` at image-build time (`scripts/install_exec_env.py`), so the
control-plane backend stays lean and the exec toolchain is its own surface.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tomllib
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "exec-env.toml")


@dataclass(frozen=True)
class ExecEnv:
    venv_dir: str
    python_version: str
    guaranteed: dict[str, str]          # distribution name -> version
    import_names: dict[str, str]        # distribution name -> import module
    system: list[str]

    @property
    def python_bin(self) -> str:
        return os.path.join(self.venv_dir, "bin", "python")

    def import_module_for(self, dist: str) -> str:
        """The importable module name for a distribution (defaults to a
        normalised form of the distribution name)."""
        return self.import_names.get(dist, dist.replace("-", "_"))

    def pip_specs(self) -> list[str]:
        """`name==version` specs for the guaranteed set (build-time install)."""
        return [f"{name}=={ver}" for name, ver in self.guaranteed.items()]


@lru_cache(maxsize=1)
def load(path: str = _MANIFEST_PATH) -> ExecEnv:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    py = data.get("python", {})
    return ExecEnv(
        venv_dir=data["meta"]["venv_dir"],
        python_version=py.get("version", "3.12"),
        guaranteed=dict(py.get("guaranteed", {})),
        import_names=dict(py.get("import_names", {})),
        system=list(data.get("system", {}).get("guaranteed", [])),
    )


def render_capabilities(env: ExecEnv | None = None) -> str:
    """The guaranteed-toolchain description the agent system prompt embeds."""
    env = env or load()
    libs = ", ".join(sorted(env.guaranteed))
    tools = ", ".join(env.system)
    return (
        f"A pinned Python {env.python_version} execution environment is available. "
        f"Guaranteed-present libraries (no install needed): {libs}. "
        f"System tools on PATH: {tools}. "
        "Any other PyPI package can be installed on demand with pip before use."
    )


def verify(env: ExecEnv | None = None, *, strict: bool = False) -> list[str]:
    """Drift-guard: confirm the exec venv contains every guaranteed library.

    Returns the list of missing/broken `dist` names. When the exec venv is not
    present (local dev / CI without the built image) verification is skipped and
    an empty list is returned — the guard is meaningful only against the built
    image. With ``strict=True`` a non-empty result raises RuntimeError.
    """
    env = env or load()
    if not os.path.exists(env.python_bin):
        logger.info("[exec-env] venv %s absent — drift check skipped (unbuilt image)", env.venv_dir)
        return []
    modules = [env.import_module_for(dist) for dist in env.guaranteed]
    dist_by_module = {env.import_module_for(dist): dist for dist in env.guaranteed}
    # One subprocess: import each module in the exec venv, print the ones that fail.
    probe = (
        "import importlib, sys\n"
        f"mods = {modules!r}\n"
        "missing = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "print('\\n'.join(missing))\n"
    )
    try:
        out = subprocess.run(
            [env.python_bin, "-c", probe],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("[exec-env] drift probe failed to run: %s", exc)
        if strict:
            raise RuntimeError(f"exec-env drift probe failed: {exc}") from exc
        return sorted(env.guaranteed)
    missing_modules = [m for m in out.stdout.splitlines() if m.strip()]
    missing = sorted(dist_by_module[m] for m in missing_modules)
    if missing:
        msg = f"exec-env drift: guaranteed libraries missing from {env.venv_dir}: {missing}"
        if strict:
            raise RuntimeError(msg)
        logger.error("[exec-env] %s", msg)
    else:
        logger.info("[exec-env] drift check ok — %d guaranteed libs present", len(env.guaranteed))
    return missing
