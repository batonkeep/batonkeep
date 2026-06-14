"""
test_mcp_provider.py — the SDK-backed MCP client ToolProvider (P-0046 slice 4).

Locks the result normalisation (text/error/non-text/empty), graceful degradation
when a server won't start, and that the curated `fetch` server is wired into the
registry + dispatchable through the dynamic post-discovery index. Live network
round-trips against the real `fetch` server are exercised manually (smoke test in
the PR), not here, to keep the suite hermetic.
"""
from __future__ import annotations

import mcp.types as mt
import pytest

from app.providers.tools.mcp_provider import McpStdioToolProvider, _normalize_result
from app.providers.tools.registry import get_tool_registry


def _result(blocks, is_error=False):
    return mt.CallToolResult(content=blocks, isError=is_error)


def test_normalize_concatenates_text_blocks():
    blocks = [mt.TextContent(type="text", text="hello"), mt.TextContent(type="text", text="world")]
    assert _normalize_result("fetch", _result(blocks)) == "hello\nworld"


def test_normalize_maps_is_error():
    r = _result([mt.TextContent(type="text", text="boom")], is_error=True)
    out = _normalize_result("fetch", r)
    assert out.startswith("[fetch error]") and "boom" in out


def test_normalize_empty_is_not_blank():
    assert _normalize_result("fetch", _result([])) == "[fetch] (no output)"


def test_normalize_non_text_block_placeholder():
    img = mt.ImageContent(type="image", data="abc", mimeType="image/png")
    assert "[image content]" in _normalize_result("fetch", _result([img]))


def test_empty_command_rejected():
    with pytest.raises(ValueError):
        McpStdioToolProvider("bad", [])


async def test_discovery_failure_degrades_gracefully():
    # A server module that doesn't exist → discover() must not raise, just expose
    # no tools (never break the registry).
    import sys

    cmd = [sys.executable, "-m", "no_such_mcp_server_xyz"]
    p = McpStdioToolProvider("ghost", cmd, sandboxed=False)
    await p.discover()
    assert p.list_tools() == []


async def test_call_on_undiscovered_server_returns_tool_error():
    import sys

    cmd = [sys.executable, "-m", "no_such_mcp_server_xyz"]
    p = McpStdioToolProvider("ghost", cmd, sandboxed=False)
    out = await p.call_tool("fetch", {"url": "https://example.com"}, workdir="/tmp")
    assert out.startswith("[fetch error]")


def test_fetch_server_wired_into_registry():
    # Constructed (not yet discovered) — the provider is present so startup
    # discovery can populate it; dispatch resolves dynamically post-discovery.
    reg = get_tool_registry()
    assert any(p.__class__.__name__ == "McpStdioToolProvider" for p in reg._providers)
