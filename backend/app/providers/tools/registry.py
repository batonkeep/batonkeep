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
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.providers.tools import file_write, flights, web_fetch, web_search


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

        policy = (context or {}).get("exec_policy")
        return await code_exec.run(workdir=workdir, policy=policy, **arguments)


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

    async def call(
        self, name: str, args_json: str, *, workdir: str, context: dict | None = None
    ) -> str:
        provider = self._index.get(name)
        if provider is None:
            return f"[unknown tool: {name}]"
        try:
            arguments = json.loads(args_json) if args_json else {}
            return await provider.call_tool(name, arguments, workdir=workdir, context=context)
        except Exception as exc:
            return f"[{name} error] {exc}"


_REGISTRY: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """The default registry — the curated first-party providers (P-0017 step 1 +
    P-0046 Tier A). Arbitrary external MCP servers (Tier B) stay gated on P-0012
    and slot in here as a further provider when that trust model lands."""
    global _REGISTRY
    if _REGISTRY is None:
        # Imported here to avoid a module-load cycle (filesystem imports from us).
        from app.providers.tools.filesystem import FilesystemToolProvider

        _REGISTRY = ToolRegistry(
            [BuiltinToolProvider(), FilesystemToolProvider(), CodeExecToolProvider()]
        )
    return _REGISTRY
