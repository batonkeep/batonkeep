"""
tests/test_workspace_exclude.py — toolchain trees stay out of turn diffs (P-0081, R3-D4).

A cold-continuity turn once produced 867 changed-file rows for three real outputs:
a `.venv` the agent built, staged whole because nothing excluded it at the commit
boundary. These verify the two halves of the fix: the rows never enter a commit,
and the fact that an environment was created is still recorded, one line per tree.
"""
from __future__ import annotations

import asyncio
import os

from app.sessions import workspace as ws


def _write(root: str, rel: str, body: str = "x") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def test_venv_is_excluded_from_the_commit_but_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False
    )
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)

    async def go():
        root = await ws.create_workspace("sx", title="X", goal="G")
        # The exclude file was seeded at init, before any add.
        exclude = os.path.join(root, ".git", "info", "exclude")
        assert os.path.exists(exclude)
        with open(exclude, encoding="utf-8") as f:
            assert ".venv/" in f.read()

        # Agent builds a real output AND a fat toolchain tree.
        _write(root, "report.md", "# result")
        for i in range(200):
            _write(root, f".venv/lib/pkg_{i}.py", "pass")
        _write(root, "node_modules/left-pad/index.js", "module.exports=0")

        version = await ws.commit_turn(root, seq=1, provider="test")
        assert version is not None
        changed = [f["path"] for f in version["files"]]
        # The real output is versioned…
        assert "report.md" in changed
        # …and not one of the 200 .venv rows leaked into the diff.
        assert not any(p.startswith(".venv") for p in changed)
        assert not any(p.startswith("node_modules") for p in changed)
        # …but the fact that they were built is recorded, per-tree.
        assert version["environments"] == [".venv", "node_modules"]

    asyncio.run(go())


def test_present_toolchain_dirs_does_not_walk_into_matched_trees(tmp_path):
    root = str(tmp_path / "ws")
    _write(root, "src/app.py", "pass")
    _write(root, ".venv/a", "x")
    _write(root, ".venv/nested/node_modules/b", "x")  # must not double-count
    assert ws.present_toolchain_dirs(root) == [".venv"]
