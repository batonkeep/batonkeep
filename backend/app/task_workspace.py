"""
task_workspace.py — per-task isolated workspaces for scheduled runs (P-0022/D-0020).

Layout (the agent's entire visible, writable world for a run):

    /work/task_<id>/
    ├── history/            ← batond-owned, read-only to the agent: prior runs'
    │   └── run_<n>/        outputs, promoted here by the orchestrator (never the
    │       └── output.md   agent) so a poisoned/buggy run can't corrupt what the
    │       └── output.json next run trusts.
    └── current/            ← the agent cwd; sandbox-writable scratch + where it
                              writes output.md/json this run.

The agent runs as the low-privilege `sandbox` user and cwd's into `current/`; it
cannot reach /app or control-plane /data (kernel DAC). After a successful run the
orchestrator copies the canonical output to /data/outputs/run_<id> (unchanged
control-plane location) AND promotes it read-only into history/.

Path resolution is traversal-safe: a task id can never escape /work.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import stat

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

# Subdirs of the agent's current/ scratch that hold deliverable artifacts, mirroring
# the session upload-in convention (sessions/uploads.py): generated images and other
# media land in assets/, agent-written data files (csv/pdf/…) in data/. Only these
# are captured as run assets — the rest of current/ is throwaway scratch (P-0050).
_ASSET_SUBDIRS = ("assets", "data")


def _task_root(task_id: int) -> str:
    """Absolute, traversal-safe /work/task_<id> root."""
    root = os.path.realpath(os.path.join(_settings.work_dir, f"task_{int(task_id)}"))
    base = os.path.realpath(_settings.work_dir)
    if not (root == base or root.startswith(base + os.sep)):
        raise ValueError(f"task workspace escapes work_dir: {root}")
    return root


def prepare_current(task_id: int) -> str:
    """Create a fresh, empty `current/` scratch for this run; return its path.

    The previous run's `current/` is discarded — its output already lives in
    history/ (promoted) and /data/outputs (canonical), so nothing is lost.
    """
    root = _task_root(task_id)
    current = os.path.join(root, "current")
    os.makedirs(os.path.join(root, "history"), exist_ok=True)
    if os.path.isdir(current):
        shutil.rmtree(current, ignore_errors=True)
    os.makedirs(current, exist_ok=True)
    return current


def latest_history(task_id: int, max_chars: int = 8000) -> str | None:
    """Most recent promoted run output (markdown) for prompt injection, or None.

    Lets a monitoring task reason about what changed since last time without the
    agent browsing the filesystem. Truncated to keep the prompt bounded.
    """
    history = os.path.join(_task_root(task_id), "history")
    if not os.path.isdir(history):
        return None
    runs = sorted(
        (d for d in os.listdir(history) if d.startswith("run_")),
        key=lambda d: int(d.split("_", 1)[1]) if d.split("_", 1)[1].isdigit() else -1,
        reverse=True,
    )
    for d in runs:
        md = os.path.join(history, d, "output.md")
        if os.path.isfile(md):
            with open(md, encoding="utf-8") as f:
                text = f.read().strip()
            return text[:max_chars] if text else None
    return None


def capture_assets(workdir: str, outputs_dir: str) -> list[dict]:
    """Copy deliverable artifacts from the agent's `current/` scratch into the run's
    canonical outputs dir, and return their metadata (P-0050/D-0046).

    Scans `assets/` and `data/` under `workdir` (the agent cwd) for files whose
    extension is on the upload allowlist and whose size is within the upload cap —
    the same gate uploads-in use, so capture and the agent's writable surface agree.
    Generated images (`image_generate` defaults into assets/) and agent-written
    csv/pdf/etc. are picked up; throwaway scratch in the current/ root is ignored.

    Returns a list of `{rel_path, mime, bytes}` (rel_path relative to outputs_dir,
    e.g. "assets/generated-1.png"). Text deliverables (output.md/json) are handled
    separately by the orchestrator and are never captured here.
    """
    allowed = _settings.upload_allowed_ext_set
    max_bytes = _settings.upload_max_bytes
    captured: list[dict] = []
    for sub in _ASSET_SUBDIRS:
        src_root = os.path.join(workdir, sub)
        if not os.path.isdir(src_root):
            continue
        for dirpath, _dirs, files in os.walk(src_root):
            for fname in sorted(files):
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                if ext not in allowed:
                    continue
                abs_src = os.path.join(dirpath, fname)
                if os.path.islink(abs_src) or not os.path.isfile(abs_src):
                    continue
                size = os.path.getsize(abs_src)
                if size == 0 or size > max_bytes:
                    continue
                rel = os.path.relpath(abs_src, workdir)  # e.g. assets/generated-1.png
                abs_dst = os.path.join(outputs_dir, rel)
                os.makedirs(os.path.dirname(abs_dst), exist_ok=True)
                shutil.copyfile(abs_src, abs_dst)
                captured.append(
                    {
                        "rel_path": rel,
                        "mime": mimetypes.guess_type(fname)[0],
                        "bytes": size,
                    }
                )
    if captured:
        logger.info("[task_workspace] captured %d run asset(s) into %s", len(captured), outputs_dir)
    return captured


def promote(task_id: int, run_id: int, src_dir: str) -> None:
    """Copy this run's canonical outputs into read-only history/run_<id>/.

    Only the orchestrator calls this (after a validated success), with the
    control-plane outputs dir as source. Copies the text deliverables
    (output.md/json) plus any captured assets under assets/ and data/. Files are
    made read-only so a later run — which the agent *can* read but must not mutate
    — cannot rewrite history.
    """
    dest = os.path.join(_task_root(task_id), "history", f"run_{int(run_id)}")
    os.makedirs(dest, exist_ok=True)
    for name in ("output.md", "output.json"):
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            dst = os.path.join(dest, name)
            shutil.copyfile(src, dst)
            os.chmod(dst, stat.S_IRUSR | stat.S_IRGRP)  # 0440 — read-only
    for sub in _ASSET_SUBDIRS:
        src_root = os.path.join(src_dir, sub)
        if not os.path.isdir(src_root):
            continue
        for dirpath, _dirs, files in os.walk(src_root):
            for fname in files:
                src = os.path.join(dirpath, fname)
                rel = os.path.relpath(src, src_dir)
                dst = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                os.chmod(dst, stat.S_IRUSR | stat.S_IRGRP)  # 0440 — read-only
