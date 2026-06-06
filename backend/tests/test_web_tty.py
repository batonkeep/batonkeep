"""
test_web_tty.py — D-0016 slice 4 / D-0017 human-driven web-TTY session builder.

Covers target resolution + gating in build_web_tty_session without a live TUI:
deployment-mode gate, unknown/invalid instance, non-CLI provider, missing
workspace, and a successful build (argv has no prompt/skip flag, cwd is the
workspace, the instance config dir is exported). The PTY plumbing itself
(PtySession) is exercised by the live console lane and not re-spawned here.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import app.web_tty as web_tty
import app.sessions.workspace as ws_mod
from app.web_tty import WebTtyError, build_web_tty_session
from app.providers.registry import ProviderDef, ProviderInstance


def _patch_settings(monkeypatch, tmp_path, *, mode="personal"):
    fake = SimpleNamespace(
        plan_cli_allowed=(mode != "managed"),
        deployment_mode=SimpleNamespace(value=mode),
        sessions_dir=str(tmp_path),
    )
    monkeypatch.setattr(web_tty, "_settings", fake)
    monkeypatch.setattr(ws_mod, "_settings", fake)
    return fake


def _patch_registry(monkeypatch, instance, pdef):
    monkeypatch.setattr(
        web_tty, "get_instance",
        lambda iid: instance if instance and iid == instance.id else None,
    )
    monkeypatch.setattr(
        web_tty, "get_provider_def",
        lambda name: pdef if pdef and name == pdef.name else None,
    )


def _make_workspace(tmp_path, session_id="sess1"):
    d = tmp_path / session_id
    d.mkdir()
    return session_id


def test_refused_in_managed_mode(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path, mode="managed")
    with pytest.raises(WebTtyError, match="plan-CLI is not available"):
        build_web_tty_session("sess1", "claude")


def test_unknown_instance(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    _patch_registry(monkeypatch, instance=None, pdef=None)
    with pytest.raises(WebTtyError, match="unknown provider instance"):
        build_web_tty_session("sess1", "nope")


def test_non_cli_provider_refused(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    inst = ProviderInstance(id="gpt", template="gpt", label="gpt")
    pdef = ProviderDef(name="gpt", kind="openai_compatible", tier="frontier")
    _patch_registry(monkeypatch, inst, pdef)
    with pytest.raises(WebTtyError, match="no interactive CLI"):
        build_web_tty_session("sess1", "gpt")


def test_missing_workspace(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    inst = ProviderInstance(id="claude", template="claude", label="claude")
    pdef = ProviderDef(name="claude", kind="cli", tier="agent", cli_binary="claude")
    _patch_registry(monkeypatch, inst, pdef)
    with pytest.raises(WebTtyError, match="workspace not found"):
        build_web_tty_session("ghost", "claude")


def test_unsafe_session_id(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    inst = ProviderInstance(id="claude", template="claude", label="claude")
    pdef = ProviderDef(name="claude", kind="cli", tier="agent", cli_binary="claude")
    _patch_registry(monkeypatch, inst, pdef)
    with pytest.raises(WebTtyError):
        build_web_tty_session("../etc", "claude")


def test_successful_build(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    sid = _make_workspace(tmp_path)
    inst = ProviderInstance(
        id="claude:work", template="claude", label="Claude (work)",
        cli_config_dir="/cfg/work", cli_config_env="CLAUDE_CONFIG_DIR",
    )
    pdef = ProviderDef(name="claude", kind="cli", tier="agent", cli_binary="claude")
    _patch_registry(monkeypatch, inst, pdef)

    session = build_web_tty_session(sid, "claude:work")

    # Human-driven: argv is just the binary — no prompt, no -p, no skip flag.
    assert session._argv == ["claude"]
    # cwd is the session workspace (resolved under sessions_dir).
    assert session._cwd == os.path.abspath(os.path.join(str(tmp_path), sid))
    # The instance's config dir is exported, and TERM is stabilised for xterm.js.
    assert session._env["CLAUDE_CONFIG_DIR"] == "/cfg/work"
    assert session._env["TERM"] == "xterm-256color"
