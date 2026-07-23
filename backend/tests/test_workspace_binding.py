"""
tests/test_workspace_binding.py — P-0083 (R4): provider work must not escape the
assigned session workspace, and setup noise must never masquerade as delivery.

Covers the three verifiable halves of the fix:
- item 3: projection ignores go into `.git/info/exclude`, never a tracked
  `.gitignore` — so a no-output turn cannot commit that file as a phantom version;
- item 2: the agy workspace-binding version-drift predicate;
- the shared idempotent exclude writer that both the toolchain seed (P-0081) and
  the projection seam (P-0083) merge into without clobbering each other.
"""
from __future__ import annotations

import asyncio
import os

from app.providers.cli_executor import agy_binding_drifted
from app.sessions import workspace as ws


def _read_exclude(root: str) -> str:
    with open(os.path.join(root, ".git", "info", "exclude"), encoding="utf-8") as f:
        return f.read()


def test_projection_ignores_go_to_git_exclude_not_gitignore(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False
    )
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)

    async def go():
        from app import project_context as pc

        root = await ws.create_workspace("sx", title="X", goal="G")
        pc._ensure_projection_excluded(root)

        # the projection entries are excluded, and NO tracked .gitignore was written
        body = _read_exclude(root)
        for entry in pc._IGNORE_ENTRIES:
            assert entry in body
        assert not os.path.exists(os.path.join(root, ".gitignore"))

        # a turn that materializes projected context + ledger but writes no
        # deliverable commits NOTHING — the R4 masquerade is gone
        os.makedirs(os.path.join(root, "context", "evidence"))
        with open(os.path.join(root, "context", "evidence", "1_x.md"), "w") as f:
            f.write("projected")
        with open(os.path.join(root, pc.LEDGER_FILENAME), "w") as f:
            f.write("# ledger")
        version = await ws.commit_turn(root, seq=1, provider="agy")
        assert version is None  # nothing stageable → no phantom version

    asyncio.run(go())


def test_toolchain_and_projection_excludes_coexist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False
    )
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)

    async def go():
        from app import project_context as pc

        root = await ws.create_workspace("sy", title="Y", goal="G")
        # create_workspace already seeded _PRUNE_DIRS; add projection entries after
        pc._ensure_projection_excluded(root)
        body = _read_exclude(root)
        # the P-0081 toolchain lines survived the P-0083 append (merge, not truncate)
        assert ".venv/" in body and "node_modules/" in body
        # and the projection lines are present too
        assert "context/" in body

    asyncio.run(go())


def test_add_git_excludes_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ws._settings, "sessions_dir", str(tmp_path / "sessions"), raising=False
    )
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)

    async def go():
        root = await ws.create_workspace("sz", title="Z", goal="G")
        ws.add_git_excludes(root, ["context/", "WORKITEM.md"])
        first = _read_exclude(root)
        ws.add_git_excludes(root, ["context/", "WORKITEM.md"])  # again
        assert _read_exclude(root) == first  # no duplicate lines

    asyncio.run(go())


def test_agy_binding_drift_predicate():
    # newer than the verified contract → drifted (the R4 case)
    assert agy_binding_drifted("agy 1.1.5") is True
    assert agy_binding_drifted("1.2.0") is True
    # the verified contract itself and older → not flagged
    assert agy_binding_drifted("agy 1.1.2") is False
    assert agy_binding_drifted("agy 1.0.12") is False
    # unknown / unparseable never blocks a lane
    assert agy_binding_drifted(None) is False
    assert agy_binding_drifted("unknown") is False
