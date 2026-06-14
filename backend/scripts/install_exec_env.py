#!/usr/bin/env python3
"""
scripts/install_exec_env.py — build the exec venv from the exec-env manifest.

Run at image-build time (Dockerfile). Reads `exec-env.toml` ([python].guaranteed)
and installs that exact set into a **separate** venv (`[meta].venv_dir`,
default /opt/exec-env/.venv) using `uv`, kept distinct from the backend's own
/app/.venv. The manifest is the single source of truth — there is no second
hardcoded package list to drift; `app.exec_env.verify()` re-checks the result at
boot (the drift-guard).

Standalone on purpose (parses the manifest directly rather than importing
`app.exec_env`) so the Dockerfile can build the exec-env layer before/independent
of copying application code — keeping the heavy install layer cacheable.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(_BACKEND_DIR, "exec-env.toml")


def main() -> int:
    with open(MANIFEST, "rb") as f:
        data = tomllib.load(f)
    venv_dir = data["meta"]["venv_dir"]
    py = data.get("python", {})
    version = py.get("version", "3.12")
    guaranteed = py.get("guaranteed", {})
    specs = [f"{name}=={ver}" for name, ver in guaranteed.items()]
    if not specs:
        print("[install_exec_env] no guaranteed packages declared; nothing to do")
        return 0

    print(f"[install_exec_env] creating exec venv at {venv_dir} (python {version})")
    subprocess.run(["uv", "venv", venv_dir, "--python", version], check=True)

    python_bin = os.path.join(venv_dir, "bin", "python")
    print(f"[install_exec_env] installing {len(specs)} guaranteed packages: {', '.join(specs)}")
    subprocess.run(
        ["uv", "pip", "install", "--python", python_bin, "--no-cache", *specs],
        check=True,
    )
    print("[install_exec_env] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
