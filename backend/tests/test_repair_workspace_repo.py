"""P-0079 repair gate: when may Batonkeep adopt a repo an agent displaced?

The maintenance script re-adopts workspace repos that agents replaced before the
`--shared=group` fix. Adoption is only safe when the agent *cloned* our repo
before displacing it, so its history is a strict superset of ours — which is what
the R3 specimens turned out to be. Anything else is a foreign history, and
adopting it would make the substrate attest to work it never mediated.

The dubious-ownership condition that triggers the gate needs two uids and cannot
be reproduced in-process; the ancestry decision can be, and it is the part that
must never be wrong.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "repair_workspace_repo",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "repair-workspace-repo.py"),
)
rw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rw)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _new_workspace(path):
    """A workspace as the harness creates it: one initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "agent@batonkeep.local")
    _git(path, "config", "user.name", "batonkeep")
    (path / "SESSION.md").write_text("brief")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "session: initialise workspace")
    return path


class TestAdoptionGate:
    def test_adopts_when_agent_cloned_our_repo_then_extended_it(self, tmp_path):
        """The R3 shape: `.git_old` is ours, `.git` is a clone plus real work."""
        ws = _new_workspace(tmp_path / "descended")
        os.rename(ws / ".git", ws / ".git_old")
        subprocess.run(["git", "clone", "-q", str(ws / ".git_old"), str(tmp_path / "c")],
                       check=True, capture_output=True)
        os.rename(tmp_path / "c" / ".git", ws / ".git")
        _git(ws, "config", "user.email", "agent@batonkeep.local")
        _git(ws, "config", "user.name", "batonkeep")
        (ws / "audit.jsonl").write_text('{"verdict":"ok"}\n')
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "agent: phase 1 audit")

        result = rw.classify_displaced(str(ws))
        assert result["state"] == "adoptable"
        assert result["action"] == "adopt"
        assert result["commits_ahead"] == 1

    def test_refuses_a_history_that_does_not_descend_from_ours(self, tmp_path):
        """An agent that started fresh instead of cloning. Its commits may be
        real work, but nothing ties them to the session we created — adopting
        them would be an attestation we cannot support."""
        ws = _new_workspace(tmp_path / "disjoint")
        os.rename(ws / ".git", ws / ".git_old")
        _git(ws, "init", "-q")
        _git(ws, "config", "user.email", "agent@batonkeep.local")
        _git(ws, "config", "user.name", "batonkeep")
        (ws / "other.md").write_text("unrelated")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "agent: unrelated history")

        result = rw.classify_displaced(str(ws))
        assert result["state"] == "foreign-disjoint"
        assert result["action"] == "manual"
        assert "NOT an ancestor" in result["why"]

    def test_refuses_when_our_original_was_not_preserved(self, tmp_path):
        """One R3 session kept no copy under the name we look for. With nothing
        to compare against, lineage is unprovable — report, never guess."""
        ws = _new_workspace(tmp_path / "noorig")
        subprocess.run(["rm", "-rf", str(ws / ".git")], check=True)
        _git(ws, "init", "-q")
        _git(ws, "config", "user.email", "agent@batonkeep.local")
        _git(ws, "config", "user.name", "batonkeep")
        (ws / "x.md").write_text("x")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "agent: own repo")

        result = rw.classify_displaced(str(ws))
        assert result["state"] == "foreign-no-original"
        assert result["action"] == "manual"

    @pytest.mark.parametrize("backup_name", [".git_old", ".git.old"])
    def test_finds_the_original_under_either_observed_name(self, tmp_path, backup_name):
        """The two R3 agents chose different names for the same manoeuvre."""
        ws = _new_workspace(tmp_path / backup_name.replace(".", "_"))
        os.rename(ws / ".git", ws / backup_name)
        assert rw.find_backup(str(ws)) == str(ws / backup_name)

    def test_healthy_workspace_needs_no_action(self, tmp_path):
        ws = _new_workspace(tmp_path / "healthy")
        result = rw.inspect(str(ws))
        assert result["state"] == "ok"
        assert result["action"] == "none"

    def test_workspace_without_a_repo_is_not_mistaken_for_displaced(self, tmp_path):
        ws = tmp_path / "bare"
        ws.mkdir()
        result = rw.inspect(str(ws))
        assert result["state"] == "no-repo"
        assert result["action"] == "none"
