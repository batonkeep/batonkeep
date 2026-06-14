"""
providers/tools/mcp_provider.py — SDK-backed MCP client ToolProvider (P-0046 slice 4).

This is the `ToolProvider` the registry was *designed* for (registry.py): it speaks
the real Model Context Protocol over the official `mcp` SDK, connecting to a curated,
Batonkeep-vetted **MCP server** launched over stdio. Slice 4 ships the first one —
the official `fetch` server (`mcp-server-fetch`) — which proves the SDK client seam
end to end (the path-prover from D-0014). Tier B (arbitrary user-supplied servers)
stays gated on the P-0012 trust model and slots in as further instances of this same
class — no rework.

Lifecycle (per-call connection, V1):
  • `discover()` runs ONCE at app startup (async lifespan) and caches the server's
    tool list, because the registry's `list_tools()` is sync but MCP is async. A
    server that fails to start contributes no tools (graceful degradation), never
    breaking the rest of the registry.
  • `call_tool()` opens a fresh stdio connection per call, runs the tool, tears the
    subprocess down. Robust and stateless (no leaked long-lived processes); the
    modest spawn latency is acceptable for V1. A persistent-session optimization can
    come later behind this same interface.

Isolation (`sandboxed` flag, per server):
  • A server whose binary the low-priv `sandbox` user can exec (e.g. one installed in
    the exec-env, reachable from `/work`) is launched through `sandbox.wrap()` — the
    same vertical privilege fence (D-0020) the agent CLIs and `code_exec` run behind,
    failing closed under REQUIRE_SANDBOX.
  • The `fetch` server is `sandboxed=False`: its binary lives in the control-plane
    venv (`/app/.venv`), which `sandbox` deliberately cannot read, so wrapping it
    would only fail to launch. It runs as `batond` — the SAME privilege posture as the
    existing in-process `web_fetch` built-in (also batond, also outbound HTTP). The
    only delta from `web_fetch` is the SSRF guard:
NOTE (SSRF residual): the official `fetch` server does its own outbound HTTP and does
NOT honour our `_ssrf` egress guard, so it can reach link-local/internal addresses the
built-in `web_fetch` blocks. Acceptable for single-tenant V1 (the agent acts for the
sole operator); tightening this (egress allowlist / proxy in front of the server) is a
tracked follow-up before any multi-tenant exposure.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app import sandbox
from app.providers.tools.registry import McpTool, ToolProvider

logger = logging.getLogger(__name__)


class McpStdioToolProvider(ToolProvider):
    """Connects to one MCP server (launched over stdio) and exposes its tools
    through the registry's `ToolProvider` interface."""

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        sandboxed: bool = True,
        extra_args: Callable[[], list[str]] | None = None,
    ) -> None:
        if not command:
            raise ValueError("McpStdioToolProvider needs a non-empty launch command")
        self.name = name
        self._command = command
        self._sandboxed = sandboxed
        # Resolved at each launch (not construction) so dynamic state — e.g. the
        # SSRF-proxy URL, which isn't known until the proxy has started — can be
        # appended to the server's argv.
        self._extra_args = extra_args
        self._tools: list[McpTool] = []

    # ── lifecycle ────────────────────────────────────────────────────────────────
    @asynccontextmanager
    async def _session(self, cwd: str | None = None):
        """A live `ClientSession` over a freshly-spawned server subprocess.

        Launches through `sandbox.wrap()` (D-0020 privilege drop; fails closed under
        REQUIRE_SANDBOX). Raises if the server can't be spawned — callers decide
        whether that's fatal (startup discovery) or a per-call tool error."""
        command = self._command + (self._extra_args() if self._extra_args else [])
        argv = sandbox.wrap(command) if self._sandboxed else command
        params = StdioServerParameters(command=argv[0], args=list(argv[1:]), cwd=cwd)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def discover(self) -> None:
        """Connect once and cache the server's tool list (called at startup).

        Failure is non-fatal: logs and leaves the tool cache empty so a missing /
        broken server degrades gracefully rather than breaking the registry."""
        try:
            async with self._session() as session:
                resp = await session.list_tools()
                self._tools = [
                    McpTool(
                        name=t.name,
                        description=t.description or "",
                        input_schema=t.inputSchema or {"type": "object", "properties": {}},
                    )
                    for t in resp.tools
                ]
            logger.info(
                "[mcp:%s] discovered %d tool(s): %s",
                self.name, len(self._tools), ", ".join(t.name for t in self._tools),
            )
        except Exception as exc:  # noqa: BLE001 — a bad server must not break startup
            logger.warning("[mcp:%s] discovery failed, no tools exposed: %s", self.name, exc)
            self._tools = []

    # ── ToolProvider ─────────────────────────────────────────────────────────────
    def list_tools(self) -> list[McpTool]:
        return list(self._tools)

    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        try:
            async with self._session(cwd=workdir) as session:
                result = await session.call_tool(name, arguments)
        except sandbox.SandboxUnavailableError as exc:
            return f"[{name} error] {exc}"
        except Exception as exc:  # noqa: BLE001 — surface as a tool error, never crash the loop
            return f"[{name} error] MCP server call failed: {exc}"
        return _normalize_result(name, result)


def _normalize_result(name: str, result) -> str:
    """Flatten an MCP `CallToolResult` to the registry's `-> str` contract.

    Concatenates text blocks; non-text blocks (image/embedded resource) become a
    short placeholder. `isError` maps onto the `[<name> error] …` convention."""
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[{getattr(block, 'type', 'non-text')} content]")
    body = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        return f"[{name} error] {body or 'MCP server reported an error'}"
    return body or f"[{name}] (no output)"
