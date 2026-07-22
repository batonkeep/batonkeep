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


class TestBootGate:
    """The gate runs from entrypoint.sh *before* `chown -R batond:agents`, which
    is the step that silently makes a displaced repo look like ours. It classifies
    by ownership because that is the only signal still present at that moment, and
    it never blocks startup.
    """

    def _displaced(self, tmp_path, name, *, descended: bool, keep_original=True):
        ws = _new_workspace(tmp_path / name)
        os.rename(ws / ".git", ws / ".git_old")
        if descended:
            subprocess.run(["git", "clone", "-q", str(ws / ".git_old"), str(tmp_path / f"c{name}")],
                           check=True, capture_output=True)
            os.rename(tmp_path / f"c{name}" / ".git", ws / ".git")
        else:
            _git(ws, "init", "-q")
        _git(ws, "config", "user.email", "agent@batonkeep.local")
        _git(ws, "config", "user.name", "batonkeep")
        (ws / "work.md").write_text("agent output")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "agent: work")
        if not keep_original:
            subprocess.run(["rm", "-rf", str(ws / ".git_old")], check=True)
        return ws

    def test_flags_a_disjoint_history_before_the_chown_adopts_it(self, tmp_path, monkeypatch):
        """The finding that motivated this: the boot pass would otherwise hand a
        foreign history to the control plane as the session's own."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        self._displaced(sessions, "disjoint", descended=False)
        report = tmp_path / "repo-provenance.json"
        flagged = rw.boot_scan(str(sessions), str(report), owner_uid=_foreign_uid())

        assert len(flagged) == 1
        assert flagged[0]["verdict"] == "disjoint"
        assert "not an ancestor" in flagged[0]["why"]
        assert report.exists()

    def test_descended_history_is_recorded_but_not_alarming(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        self._displaced(sessions, "descended", descended=True)
        flagged = rw.boot_scan(str(sessions), str(tmp_path / "r.json"), owner_uid=_foreign_uid())
        assert len(flagged) == 1
        assert flagged[0]["verdict"] == "descended"
        assert flagged[0]["commits_ahead"] == 1

    def test_missing_original_is_unprovable_not_assumed_fine(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        self._displaced(sessions, "noorig", descended=False, keep_original=False)
        flagged = rw.boot_scan(str(sessions), str(tmp_path / "r.json"), owner_uid=_foreign_uid())
        assert flagged[0]["verdict"] == "unprovable"

    def test_healthy_tree_writes_no_report_and_clears_a_stale_one(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _new_workspace(sessions / "healthy")

        report = tmp_path / "repo-provenance.json"
        report.write_text('{"flagged": [{"session": "resolved-since"}]}')
        # owner_uid = us, so every repo here is "ours" and nothing is flagged.
        assert rw.boot_scan(str(sessions), str(report), owner_uid=os.geteuid()) == []
        # A stale report must not keep accusing a session that has been resolved.
        assert not report.exists()

    def test_the_app_reads_the_report_the_gate_writes(self, tmp_path, monkeypatch):
        """Contract between entrypoint and app: a report nobody surfaces is not a gate."""
        from app.sessions import workspace as ws

        report = tmp_path / "repo-provenance.json"
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        self._displaced(sessions, "disjoint2", descended=False)
        rw.boot_scan(str(sessions), str(report), owner_uid=_foreign_uid())

        monkeypatch.setenv("REPO_PROVENANCE_REPORT", str(report))
        surfaced = ws.flagged_repos()
        assert [e["verdict"] for e in surfaced] == ["disjoint"]

    def test_absent_report_is_the_normal_case(self, monkeypatch, tmp_path):
        from app.sessions import workspace as ws
        monkeypatch.setenv("REPO_PROVENANCE_REPORT", str(tmp_path / "nope.json"))
        assert ws.flagged_repos() == []


def _foreign_uid() -> int:
    """A uid that owns nothing in the fixture, so every `.git` reads as
    agent-written — the condition the boot gate exists to classify. Tests own
    every file they create, so the ownership signal has to be supplied."""
    return os.geteuid() + 1
