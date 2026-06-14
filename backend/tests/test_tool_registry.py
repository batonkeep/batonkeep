"""
test_tool_registry.py — the MCP-shaped agent-tool seam (P-0017).

Locks the contract the model executor depends on: the registry lists the
built-in tools as MCP descriptors, exposes them as function schemas, dispatches
by name, injects `workdir` into `file_write`, and degrades gracefully on
unknown tools / bad args.
"""
from __future__ import annotations

import json

from app.providers.tools.registry import (
    BuiltinToolProvider,
    McpTool,
    ToolRegistry,
    get_tool_registry,
)

BUILTIN_NAMES = {"web_search", "web_fetch", "flights", "file_write"}


def test_lists_builtin_tools_as_mcp_descriptors():
    reg = ToolRegistry([BuiltinToolProvider()])
    tools = reg.list_tools()
    assert {t.name for t in tools} == BUILTIN_NAMES
    for t in tools:
        assert isinstance(t, McpTool)
        assert t.description
        assert t.input_schema["type"] == "object"  # MCP inputSchema is JSON Schema


def test_function_schemas_match_executor_shape():
    schemas = get_tool_registry().function_schemas()
    names = {s["name"] for s in schemas}
    # Default registry carries the built-ins plus the P-0046 Tier-A filesystem tools.
    assert BUILTIN_NAMES <= names
    assert {"fs_read", "fs_list", "fs_glob", "fs_grep"} <= names
    for s in schemas:
        assert set(s.keys()) == {"name", "description", "parameters"}


async def test_unknown_tool_is_reported_not_raised():
    reg = get_tool_registry()
    out = await reg.call("does_not_exist", "{}", workdir="/tmp")
    assert out == "[unknown tool: does_not_exist]"


async def test_bad_args_degrade_to_error_string():
    reg = get_tool_registry()
    out = await reg.call("flights", "{not valid json", workdir="/tmp")
    assert out.startswith("[flights error]")


async def test_file_write_receives_workdir(tmp_path):
    reg = get_tool_registry()
    args = json.dumps({"filename": "out.txt", "content": "hello"})
    out = await reg.call("file_write", args, workdir=str(tmp_path))
    assert (tmp_path / "out.txt").read_text() == "hello"
    assert "wrote 5 chars" in out


async def test_first_provider_wins_on_name_collision():
    class Dummy(BuiltinToolProvider):
        async def call_tool(self, name, arguments, *, workdir):
            return "from-dummy"

    reg = ToolRegistry([BuiltinToolProvider(), Dummy()])
    # built-in flights provider claimed the name first; dummy is shadowed
    out = await reg.call("flights", json.dumps(
        {"origin": "SYD", "destination": "LHR", "date_window": "x"}), workdir="/tmp")
    assert out != "from-dummy"
    assert "Flight search" in out
