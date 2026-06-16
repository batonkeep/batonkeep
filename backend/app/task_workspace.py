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

# API-path agents save media to assets/ and data files to data/ (the session
# upload-in convention, sessions/uploads.py) — the system prompt steers them there.
# CLI-lane agents (agy/grok/claude/codex) get no such tool or guidance and save the
# file wherever they choose (usually the cwd root), so capture scans the *whole*
# run scratch, not just these subdirs (P-0050). These remain the canonical homes.
_ASSET_SUBDIRS = ("assets", "data")

# Dirs never worth scanning for run assets: VCS, dependency/build trees, caches.
# Plus any hidden dir (leading dot). Keeps a CLI agent that clones/builds something
# from flooding history with library media.
_ASSET_SCAN_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", ".git", ".cache", ".venv", "venv",
    "dist", "build", ".next", "site-packages", ".pytest_cache",
})

# Extensions captured as *assets*. Deliberately broader than the upload-in allowlist
# (which is a *user-upload* security boundary, not an agent-output one): CLI agents
# (agy/grok) generate media natively — images, video, audio — and build sessions
# already serve any such file via the Files tab, so task capture must be just as
# media-inclusive. Code/text/report scratch is intentionally excluded (output.md is
# the report; *.md/*.json/source files are not assets). Lowercased, no dots.
_ASSET_EXT = frozenset({
    # images
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "tiff", "tif", "avif", "ico", "heic",
    # video
    "mp4", "webm", "mov", "avi", "mkv", "m4v", "gifv",
    # audio
    "mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "mid", "midi",
    # documents / data deliverables
    "pdf", "csv", "tsv", "xlsx", "xls", "docx", "pptx", "parquet",
})

# Safety bound on how many assets one run can capture (a misbehaving CLI agent that
# wrote a tree of images shouldn't promote hundreds of files into history).
_ASSET_CAPTURE_MAX = 50

# Per-file capture cap. Generous (vs the 10 MiB user-upload cap) because generated
# video/audio is legitimately large; per-task retention byte budgets bound the rest.
_ASSET_MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MiB


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

    Scans the **whole** run scratch (not just assets/+data/) so it catches files
    wherever an agent saved them: API-path agents are steered to assets/, but
    CLI-lane agents (agy/grok/claude/codex) generate media natively and typically
    save it to the cwd root. Files are gated by the media/document asset extension
    set (`_ASSET_EXT` — images/video/audio/docs, *not* the narrower user-upload
    allowlist) and a generous per-file size cap, with VCS/dependency/cache dirs skipped so a
    CLI agent that cloned/built something can't flood history. Bounded to
    `_ASSET_CAPTURE_MAX` files (largest-first when over).

    Returns `{rel_path, mime, bytes}` per asset (rel_path relative to outputs_dir,
    preserving the source layout, e.g. "assets/generated-1.png" or "daily-image.png").
    Text deliverables (output.md/json) are handled separately by the orchestrator.
    """
    allowed = _ASSET_EXT
    max_bytes = _ASSET_MAX_FILE_BYTES
    found: list[tuple[str, int]] = []  # (abs_src, size)
    for dirpath, dirs, files in os.walk(workdir):
        # Prune noise dirs + hidden dirs in place so os.walk doesn't descend them.
        dirs[:] = [d for d in dirs if d not in _ASSET_SCAN_SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext not in allowed:
                continue
            abs_src = os.path.join(dirpath, fname)
            if os.path.islink(abs_src) or not os.path.isfile(abs_src):
                continue
            size = os.path.getsize(abs_src)
            if size == 0 or size > max_bytes:
                continue
            found.append((abs_src, size))

    # Deterministic order; if over the cap keep the largest (the real deliverables
    # over incidental thumbnails/icons).
    found.sort(key=lambda t: (-t[1], t[0]))
    found = found[:_ASSET_CAPTURE_MAX]
    found.sort(key=lambda t: t[0])

    captured: list[dict] = []
    for abs_src, size in found:
        rel = os.path.relpath(abs_src, workdir)
        abs_dst = os.path.join(outputs_dir, rel)
        os.makedirs(os.path.dirname(abs_dst) or outputs_dir, exist_ok=True)
        shutil.copyfile(abs_src, abs_dst)
        captured.append({
            "rel_path": rel,
            "mime": mimetypes.guess_type(abs_src)[0],
            "bytes": size,
        })
    if captured:
        logger.info("[task_workspace] captured %d run asset(s) into %s", len(captured), outputs_dir)
    return captured


def promote(task_id: int, run_id: int, src_dir: str) -> None:
    """Copy this run's canonical outputs into read-only history/run_<id>/.

    Only the orchestrator calls this (after a validated success), with the
    control-plane outputs dir as source. `src_dir` holds *only* what we wrote
    (output.md/json + captured assets — the agent can't write the control-plane
    outputs dir), so we copy the whole tree. Files are made read-only so a later
    run — which the agent *can* read but must not mutate — cannot rewrite history.
    """
    dest = os.path.join(_task_root(task_id), "history", f"run_{int(run_id)}")
    os.makedirs(dest, exist_ok=True)
    for dirpath, _dirs, files in os.walk(src_dir):
        for fname in files:
            src = os.path.join(dirpath, fname)
            rel = os.path.relpath(src, src_dir)
            dst = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst) or dest, exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, stat.S_IRUSR | stat.S_IRGRP)  # 0440 — read-only
