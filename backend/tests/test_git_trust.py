"""
tests/test_git_trust.py — per-workspace git trust for agent processes.

Agent workspaces are git-init'd by the control-plane user while agent processes
run as the low-privilege sandbox user, so agent `git` calls trip git's
dubious-ownership check (safe.directory). `sandbox.git_trust_env()` scopes the
exception to exactly one workspace via GIT_CONFIG_* env; these tests pin the
mapping and its injection at every agent spawn seam (headless CLI, interactive
CLI, web-TTY session console, code_exec) — and that the sessionless /tmp
console deliberately does NOT get it.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app import sandbox
from app.providers.base import EventKind
from app.providers.cli_executor import CLIExecutor
from app.providers.registry import ProviderDef


def test_git_trust_env_shape(tmp_path):
    env = sandbox.git_trust_env(str(tmp_path))
    assert env == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": os.path.realpath(str(tmp_path)),
    }


def test_git_trust_env_resolves_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    env = sandbox.git_trust_env(str(link))
    # git compares the repo's real path — a symlinked workdir must not miss.
    assert env["GIT_CONFIG_VALUE_0"] == os.path.realpath(str(real))


def test_git_trust_env_empty_for_no_workdir():
    assert sandbox.git_trust_env(None) == {}
    assert sandbox.git_trust_env("") == {}


def test_git_trust_env_actually_satisfies_git(tmp_path):
    """Functional floor: with the env applied, git accepts a repo whose path is
    listed — and (ownership aside) the env alone never *breaks* a normal repo."""
    import subprocess

    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {**os.environ, **sandbox.git_trust_env(str(repo))}
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr


@pytest.mark.asyncio
async def test_cli_executor_injects_git_trust(monkeypatch, tmp_path):
    """The headless lane's spawn env carries the workspace trust triple."""
    monkeypatch.setattr(sandbox, "available", lambda: False)
    captured: dict = {}

    class _Stream:
        def __aiter__(self):
            async def _gen():
                if False:  # pragma: no cover
                    yield b""
            return _gen()

    class _Proc:
        stdout = _Stream()
        stderr = _Stream()
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    executor = CLIExecutor(
        ProviderDef(name="claude", kind="cli", tier="agent", cli_binary="claude")
    )
    events = [ev async for ev in executor.run_stream("hi", workdir=str(tmp_path))]

    env = captured["env"]
    assert env is not None
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == os.path.realpath(str(tmp_path))
    # The run still terminates (empty output → plain-text fallback or error, not a hang).
    assert any(ev.kind in (EventKind.result, EventKind.error) for ev in events)


def test_web_tty_session_env_has_trust_but_console_does_not(monkeypatch, tmp_path):
    """The session console (real workspace) is trusted; the sessionless /tmp
    console must stay untrusted — /tmp is world-writable."""
    from types import SimpleNamespace

    import app.sessions.workspace as ws_mod
    import app.web_tty as web_tty
    from app.providers.registry import ProviderInstance
    from app.web_tty import build_console_tty_session, build_web_tty_session

    fake = SimpleNamespace(
        plan_cli_allowed=True,
        deployment_mode=SimpleNamespace(value="personal"),
        sessions_dir=str(tmp_path),
    )
    monkeypatch.setattr(web_tty, "_settings", fake)
    monkeypatch.setattr(ws_mod, "_settings", fake)

    sid = "sess1"
    (tmp_path / sid).mkdir()
    inst = ProviderInstance(id="claude", template="claude", label="claude")
    pdef = ProviderDef(name="claude", kind="cli", tier="agent", cli_binary="claude")
    monkeypatch.setattr(web_tty, "get_instance", lambda _id: inst)
    monkeypatch.setattr(web_tty, "get_provider_def", lambda _t: pdef)

    session = build_web_tty_session(sid, "claude")
    ws = os.path.join(str(tmp_path), sid)
    assert session._env["GIT_CONFIG_VALUE_0"] == os.path.realpath(ws)

    console = build_console_tty_session("claude")
    assert "GIT_CONFIG_COUNT" not in console._env


@pytest.mark.asyncio
async def test_code_exec_env_injects_git_trust(monkeypatch, tmp_path):
    """Agent-authored code sees the same workspace trust as the CLI lanes."""
    from app.providers.tools import code_exec

    captured: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = await code_exec.run("print('hi')", workdir=str(tmp_path), policy="auto")
    env = captured["env"]
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == os.path.realpath(str(tmp_path))
    assert "[code_exec]" in out
