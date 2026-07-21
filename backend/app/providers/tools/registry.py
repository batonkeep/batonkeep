"""
providers/tools/registry.py — the agent-tool seam, modelled on MCP.

The model executor (`model_executor.py`) talks to tools *only* through this
registry. Tools are described and dispatched with MCP semantics — each tool has
a `name`, a `description`, and an `input_schema` (JSON Schema, MCP's
`inputSchema`); a call takes a name + arguments dict and returns text content.

Why a seam, not the `mcp` SDK runtime (yet)?
  Per ops P-0017, we adopt MCP as the *tool interface*. Step 1 (this module)
  wraps our built-in tools behind that interface with **no behavior change** and
  **no new dependency** — the full `mcp` SDK currently pins `starlette`/`pydantic`
  versions that conflict with our `fastapi` pin. The SDK-backed provider that
  connects to *external* MCP servers lands when the first such server is actually
  needed (research/data), and is gated on the P-0012 sandbox/trust model — an
  external MCP server is untrusted code with the same threat profile as an
  uningested skill. When that lands it implements the same `ToolProvider`
  interface below and slots into `ToolRegistry` alongside the built-ins.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.providers.tools import file_write, flights, web_fetch, web_search

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpTool:
    """An MCP-shaped tool descriptor."""

    name: str
    description: str
    input_schema: dict  # JSON Schema — MCP's `inputSchema`

    def as_function_schema(self) -> dict:
        """The `{name, description, parameters}` shape the executor feeds to
        the OpenAI / Anthropic tool-conversion code."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }


class ToolProvider(ABC):
    """A source of tools. The built-in provider wraps our in-process tools;
    a future SDK-backed provider wraps a connected external MCP server."""

    @abstractmethod
    def list_tools(self) -> list[McpTool]:
        ...

    @abstractmethod
    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        """Dispatch a tool call and return text content. `workdir` is the
        session sandbox dir — built-in `file_write` writes into it; external
        providers will be launched with it as their cwd. `context` carries
        per-run dispatch state (e.g. the code-exec execution policy, P-0046);
        providers that don't need it ignore it.

        External-MCP-provider contract (validated against the real SDK + the
        official `fetch` server, P-0017): the SDK returns a `CallToolResult`
        with `.content` (a list of typed blocks — TextContent / image /
        embedded resource) and `.isError: bool`, not a plain string. The
        external provider adapter is responsible for normalising that to this
        `-> str` contract: flatten text blocks (placeholder non-text blocks),
        and map `isError is True` onto the registry's `[<name> error] …`
        convention. Launch servers with clean stdout (they may emit non-JSONRPC
        noise; the SDK logs and continues, but a clean stream is preferred)."""
        ...


class BuiltinToolProvider(ToolProvider):
    """Wraps the in-process tool modules behind the MCP interface."""

    def __init__(self) -> None:
        self._modules = {
            "web_search": web_search,
            "web_fetch": web_fetch,
            "flights": flights,
            "file_write": file_write,
        }

    def list_tools(self) -> list[McpTool]:
        tools = []
        for mod in self._modules.values():
            s = mod.TOOL_SCHEMA
            tools.append(
                McpTool(name=s["name"], description=s["description"], input_schema=s["parameters"])
            )
        return tools

    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        mod = self._modules[name]  # KeyError handled by the registry
        if name == "file_write":
            return await mod.run(**arguments, workdir=workdir)
        return await mod.run(**arguments)


class CodeExecToolProvider(ToolProvider):
    """Runs Python in the pinned exec env, gated by the execution policy (P-0046).

    Listed/dispatched here for a single tool surface, but the executor only
    *offers* `code_exec` to the model when the run's policy permits (see
    `code_exec.policy_offers_tool`); on dispatch the policy is enforced again
    (defense in depth) from `context['exec_policy']`.
    """

    def list_tools(self) -> list[McpTool]:
        from app.providers.tools import code_exec

        s = code_exec.TOOL_SCHEMA
        return [McpTool(name=s["name"], description=s["description"], input_schema=s["parameters"])]

    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        from app.providers.tools import code_exec

        ctx = context or {}
        return await code_exec.run(
            workdir=workdir, policy=ctx.get("exec_policy"),
            approve=ctx.get("approve"), **arguments,
        )


class ImageGenToolProvider(ToolProvider):
    """Capability-gated image generation (P-0046 slice 6 / P-0037).

    Dispatchable through the registry, but the executor only *offers*
    `image_generate` to the model when the active provider declares
    `supports_image_gen` (see `_active_tool_schemas`). On dispatch the per-run
    image config (credential / base_url / model / cost) arrives via `context`.
    """

    def list_tools(self) -> list[McpTool]:
        from app.providers.tools import image_gen

        s = image_gen.TOOL_SCHEMA
        return [McpTool(name=s["name"], description=s["description"], input_schema=s["parameters"])]

    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        from app.providers.tools import image_gen

        ctx = context or {}
        return await image_gen.run(
            workdir=workdir, config=ctx.get("image_gen"), **arguments,
        )


class PlannerToolProvider(ToolProvider):
    """The per-project planner agent's toolset (P-0078) — DB-state meta-work, not
    workdir work. Offered to the model *only* on a planning turn (the executor gates
    on `extra['planning']`); the tools take their WorkItem/Project scope from the run
    `context` the executor threads through dispatch. Proposer-only: never approves
    durable truth. Slice 1 = propose_subtasks + set_next_action."""

    def list_tools(self) -> list[McpTool]:
        from app.providers.tools import planner_tools

        return [
            McpTool(name=s["name"], description=s["description"], input_schema=s["parameters"])
            for s in (planner_tools.PROPOSE_SUBTASKS_SCHEMA, planner_tools.SET_NEXT_ACTION_SCHEMA)
        ]

    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        from app.providers.tools import planner_tools

        if name == "propose_subtasks":
            return await planner_tools.propose_subtasks(**arguments, context=context)
        if name == "set_next_action":
            return await planner_tools.set_next_action(**arguments, context=context)
        return f"[unknown planner tool: {name}]"


#: Names offered only on a planning turn — excluded from every non-planning run's
#: toolset (mirrors the code_exec / image_generate gating in model_executor).
PLANNER_TOOL_NAMES = ("propose_subtasks", "set_next_action")


class ToolRegistry:
    """Aggregates tool providers and presents a single dispatch surface to the
    executor. Tool names are unique; the first provider to claim a name wins."""

    def __init__(self, providers: list[ToolProvider]) -> None:
        self._providers = providers
        self._index: dict[str, ToolProvider] = {}
        for provider in providers:
            for tool in provider.list_tools():
                self._index.setdefault(tool.name, provider)

    def list_tools(self) -> list[McpTool]:
        seen: set[str] = set()
        tools: list[McpTool] = []
        for provider in self._providers:
            for tool in provider.list_tools():
                if tool.name not in seen:
                    seen.add(tool.name)
                    tools.append(tool)
        return tools

    def function_schemas(self) -> list[dict]:
        """Executor-facing tool schemas (OpenAI/Anthropic conversion input)."""
        return [t.as_function_schema() for t in self.list_tools()]

    def _resolve(self, name: str) -> ToolProvider | None:
        """Provider owning `name`. Falls back to a live scan when the name isn't in
        the prebuilt index — SDK-backed providers populate their tools at startup
        *after* construction, so their names aren't known when the index is built."""
        provider = self._index.get(name)
        if provider is not None:
            return provider
        for p in self._providers:
            if any(t.name == name for t in p.list_tools()):
                self._index.setdefault(name, p)
                return p
        return None

    async def call(
        self, name: str, args_json: str, *, workdir: str, context: dict | None = None
    ) -> str:
        provider = self._resolve(name)
        if provider is None:
            return f"[unknown tool: {name}]"
        try:
            arguments = json.loads(args_json) if args_json else {}
            return await provider.call_tool(name, arguments, workdir=workdir, context=context)
        except Exception as exc:
            return f"[{name} error] {exc}"


_REGISTRY: ToolRegistry | None = None
_MCP_PROVIDERS: list = []  # SDK-backed providers needing async startup discovery


def _build_fetch_provider():
    """The curated Tier-A `fetch` MCP server (P-0046 slice 4), launched over stdio
    from the backend venv. Non-sandboxed (its binary lives in the control-plane
    venv `sandbox` can't exec) — same batond posture as the in-process `web_fetch`
    built-in (see mcp_provider.py).

    Egress is fenced by our SSRF forward proxy: the server is launched with
    `--proxy-url` pointed at it (when started), so its outbound HTTP inherits the
    same `_ssrf` allow/deny policy as `web_fetch` (no link-local/internal reach)."""
    import sys

    from app.providers.tools import ssrf_proxy
    from app.providers.tools.mcp_provider import McpStdioToolProvider

    def proxy_args() -> list[str]:
        url = ssrf_proxy.current_url()
        return ["--proxy-url", url] if url else []

    return McpStdioToolProvider(
        "fetch",
        [sys.executable, "-m", "mcp_server_fetch"],
        sandboxed=False,
        extra_args=proxy_args,
    )


def get_tool_registry() -> ToolRegistry:
    """The default registry — the curated first-party providers (P-0017 step 1 +
    P-0046 Tier A). Arbitrary external MCP servers (Tier B) stay gated on P-0012
    and slot in here as a further provider when that trust model lands."""
    global _REGISTRY
    if _REGISTRY is None:
        # Imported here to avoid a module-load cycle (filesystem imports from us).
        from app.providers.tools.filesystem import FilesystemToolProvider

        fetch = _build_fetch_provider()
        _MCP_PROVIDERS.append(fetch)
        _REGISTRY = ToolRegistry(
            [BuiltinToolProvider(), FilesystemToolProvider(), CodeExecToolProvider(),
             ImageGenToolProvider(), PlannerToolProvider(), fetch]
        )
    return _REGISTRY


async def discover_mcp_tools() -> None:
    """Connect to each SDK-backed MCP server once and cache its tool list (called at
    app startup). The registry's `list_tools()` is sync but MCP is async, so the live
    discovery happens here; a server that won't start contributes no tools rather than
    breaking the registry."""
    # Start the SSRF egress fence before any MCP server launches so the fetch
    # server's `--proxy-url` resolves to it (curated servers fail closed to no-proxy
    # only if the proxy can't start — logged below).
    from app.providers.tools import ssrf_proxy

    try:
        await ssrf_proxy.ensure_started()
    except Exception:
        logger.exception("SSRF egress proxy failed to start")

    get_tool_registry()  # ensure providers are constructed
    for provider in _MCP_PROVIDERS:
        await provider.discover()
