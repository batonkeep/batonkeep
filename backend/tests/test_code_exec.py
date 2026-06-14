"""
test_code_exec.py — the code-exec tool + execution policy (P-0046 slice 3a).

Locks the policy gate (off/confirmation/allow-safe/auto), the allow-safe
heuristic, actual execution against the (fallback) interpreter, and the
executor's per-run tool offering.
"""
from __future__ import annotations

import json

import pytest

from app.providers.tools import code_exec
from app.providers.tools.code_exec import policy_offers_tool
from app.providers.tools.registry import CodeExecToolProvider, get_tool_registry


def test_policy_offers_tool_only_runnable():
    assert policy_offers_tool("allow-safe") is True
    assert policy_offers_tool("auto") is True
    assert policy_offers_tool("confirmation") is False
    assert policy_offers_tool("off") is False
    assert policy_offers_tool(None) is False  # default confirmation


async def test_off_is_refused(tmp_path):
    out = await code_exec.run("print(1)", workdir=str(tmp_path), policy="off")
    assert "disabled" in out


async def test_confirmation_refused_without_channel(tmp_path):
    out = await code_exec.run("print(1)", workdir=str(tmp_path), policy="confirmation")
    assert "requires operator approval" in out


async def test_auto_executes(tmp_path):
    out = await code_exec.run("print(6 * 7)", workdir=str(tmp_path), policy="auto")
    assert "42" in out
    assert out.startswith("[code_exec]")


async def test_allow_safe_runs_safe_code(tmp_path):
    out = await code_exec.run("print('ok')", workdir=str(tmp_path), policy="allow-safe")
    assert "ok" in out


async def test_allow_safe_blocks_network(tmp_path):
    out = await code_exec.run("import socket", workdir=str(tmp_path), policy="allow-safe")
    assert "blocked by allow-safe" in out


async def test_auto_allows_network_import_to_execute(tmp_path):
    # auto trusts the operator; the import itself is harmless and should run.
    out = await code_exec.run(
        "import socket; print('net-ok')", workdir=str(tmp_path), policy="auto"
    )
    assert "net-ok" in out


async def test_nonzero_exit_reported(tmp_path):
    out = await code_exec.run("raise SystemExit(3)", workdir=str(tmp_path), policy="auto")
    assert "exit 3" in out


async def test_writes_into_workdir(tmp_path):
    code = "open('artifact.txt', 'w').write('hello')"
    await code_exec.run(code, workdir=str(tmp_path), policy="auto")
    assert (tmp_path / "artifact.txt").read_text() == "hello"


async def test_dispatch_through_registry_passes_policy(tmp_path):
    out = await get_tool_registry().call(
        "code_exec", json.dumps({"code": "print(2+2)"}),
        workdir=str(tmp_path), context={"exec_policy": "auto"},
    )
    assert "4" in out


async def test_dispatch_default_policy_refuses(tmp_path):
    # No context → default confirmation → refused.
    out = await get_tool_registry().call(
        "code_exec", json.dumps({"code": "print(1)"}), workdir=str(tmp_path),
    )
    assert "requires operator approval" in out


def test_provider_lists_code_exec():
    names = {t.name for t in CodeExecToolProvider().list_tools()}
    assert names == {"code_exec"}


@pytest.mark.parametrize("policy,offered", [
    ("off", False), ("confirmation", False), ("allow-safe", True), ("auto", True),
])
def test_executor_offers_code_exec_by_policy(policy, offered):
    from app.providers.model_executor import _active_tool_schemas

    names = {s["name"] for s in _active_tool_schemas({"exec_policy": policy})}
    assert ("code_exec" in names) is offered
    # the base tools are always present
    assert {"fs_read", "web_search"} <= names
