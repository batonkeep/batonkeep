"""
providers/model_executor.py — OpenAI-compatible + Anthropic model executor (§6).

Our own agent loop:
  system prompt → model → if tool_calls → dispatch tools → feed results → repeat
  Cap rounds with max_rounds / max cost with budget_usd.

Emits the same ExecEvent sequence as MockExecutor so the orchestrator
and WS pipeline are backend-agnostic.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.providers.base import (
    EventKind,
    ExecEvent,
    ExecResult,
    Executor,
    Usage,
)
from app.providers.registry import ProviderDef, ProviderInstance

# Tools are reached only through the MCP-shaped registry (P-0017). The executor
# never imports a tool module directly — it lists schemas and dispatches calls
# through the registry, so a future external-MCP-server provider drops in
# transparently.
from app.providers.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

# ── Tool registry ─────────────────────────────────────────────────────────────
TOOL_SCHEMAS = get_tool_registry().function_schemas()

# Some tools are dispatchable through the registry but only *offered* to the model
# under per-run conditions: `code_exec` (P-0046) when the execution policy permits;
# `image_generate` (slice 6 / P-0037) when the active provider is image-capable. The
# base set excludes both; `_active_tool_schemas` re-adds each per-run.
_CODE_EXEC_NAME = "code_exec"
_IMAGE_GEN_NAME = "image_generate"
_GATED_TOOL_NAMES = {_CODE_EXEC_NAME, _IMAGE_GEN_NAME}
_BASE_TOOL_SCHEMAS = [s for s in TOOL_SCHEMAS if s["name"] not in _GATED_TOOL_NAMES]
_CODE_EXEC_SCHEMA = next((s for s in TOOL_SCHEMAS if s["name"] == _CODE_EXEC_NAME), None)
_IMAGE_GEN_SCHEMA = next((s for s in TOOL_SCHEMAS if s["name"] == _IMAGE_GEN_NAME), None)


def _active_tool_schemas(extra: dict | None) -> list[dict]:
    """The tool schemas offered for a run: the base set plus `code_exec` when the
    run's execution policy permits (P-0046), plus `image_generate` when the active
    provider is image-capable (slice 6 / P-0037)."""
    from app.providers.tools.code_exec import policy_offers_tool

    extra = extra or {}
    schemas = list(_BASE_TOOL_SCHEMAS)
    if _CODE_EXEC_SCHEMA and policy_offers_tool(
        extra.get("exec_policy"), bool(extra.get("human_in_loop"))
    ):
        schemas.append(_CODE_EXEC_SCHEMA)
    if _IMAGE_GEN_SCHEMA and extra.get("image_gen"):
        schemas.append(_IMAGE_GEN_SCHEMA)
    return schemas

_SYSTEM_PROMPT = (
    "You are an autonomous research agent. "
    "Use available tools (web_search/web_fetch for research; flights for fare queries) "
    "to gather and verify information. "
    "Research efficiently: a focused handful of searches is enough — once you have "
    "sufficient material, stop calling tools and write the report. Do not exhaustively "
    "search the same topic. "
    "Produce one polished **Markdown** report: `#` title, 2–3 sentence executive summary, "
    "then organised sections with inline source links."
)


def _base_system_prompt(extra: dict | None) -> str:
    """The system prompt for a run — `_SYSTEM_PROMPT` plus the exec-env capability
    blurb when code-exec is offered (P-0046), so the model knows the pinned
    toolchain it can rely on rather than probing for it."""
    from app.providers.tools.code_exec import policy_offers_tool

    prompt = _SYSTEM_PROMPT
    if extra and extra.get("image_gen"):
        prompt += (
            " You can generate images with the image_generate tool; saved images "
            "render in the preview pane, so create visuals directly when asked."
        )
    if not (extra and policy_offers_tool(extra.get("exec_policy"))):
        return prompt
    from app.exec_env import render_capabilities

    return f"{prompt} You can also run Python via code_exec. {render_capabilities()}"

# When the agent loop reaches its last permitted round (or trips the budget) the model
# is given one final, tool-free turn so it MUST synthesise a complete answer from what it
# has gathered. This nudge is appended to the system prompt on that turn.
_SYNTHESIS_NUDGE = (
    " You have gathered enough — stop researching now and write the complete final "
    "report from the material you already have."
)

# The system-prompt nudge alone is not reliable mid-conversation: validated live on
# claude-api, a forced tool-free turn whose history ends in tool_results frequently
# returns an EMPTY message (stop=end_turn, no blocks) when only the system text asks
# for synthesis — the same history answers fully when the instruction arrives as a
# user turn. So on the forced round we also append this as a user message (only when
# tool history exists; a bare first-round prompt needs no redirect).
_SYNTHESIS_USER_MSG = (
    "Stop researching. Using only what you have gathered above, write the complete "
    "final Markdown report now."
)


class ModelExecutor(Executor):
    """OpenAI-compatible or Anthropic model backend with our own agent loop."""

    def __init__(self, provider_def: ProviderDef, instance: ProviderInstance | None = None) -> None:
        self._def = provider_def
        self._instance = instance
        # name is the instance id so run records / cooldown key per-account.
        self.name = instance.id if instance else provider_def.name
        self.tier = provider_def.tier
        # Per-account overrides (Phase B): which model + which stored credential.
        # Runtime model override (set from the UI console) wins over the declared
        # instance/template default.
        from app.providers.registry import get_model_override
        runtime_model = get_model_override(instance.id) if instance else None
        self._model = (
            runtime_model
            or (instance.model_override if instance and instance.model_override else None)
            or provider_def.model
        )
        self._cred_provider = (
            instance.credential_provider
            if instance and instance.credential_provider
            else provider_def.name
        )

    @property
    def kind(self) -> str:
        return self._def.kind

    def is_healthy(self) -> bool:
        import os
        if self._def.env_key:
            return bool(os.environ.get(self._def.env_key))
        return True

    def _compute_cost(self, usage: Usage) -> float:
        # Meter at the *effective* rate for the model actually in use: operator
        # pricing override > known-model price book > template default. Pinning to
        # the template made an overridden/custom model bill at the wrong rate.
        from app.providers import model_pricing
        from app.providers.registry import effective_pricing

        in_rate, out_rate = effective_pricing(
            self._def, self._instance.id if self._instance else None, self._model
        )
        # Cache-read / cache-write tokens bill at their own rates (cheap reads, a
        # write premium). They are 0 unless caching is on, so this reduces to the
        # old in/out formula on the non-cached paths. `tokens_in` already excludes
        # the cached portion (the loops capture them separately), so the three add.
        cache_read_rate, cache_write_rate = model_pricing.cache_rates(self._model, in_rate)
        return (
            usage.tokens_in * in_rate / 1_000_000
            + usage.tokens_out * out_rate / 1_000_000
            + usage.cache_read_tokens * cache_read_rate / 1_000_000
            + usage.cache_write_tokens * cache_write_rate / 1_000_000
        )

    def _aux_cost(self) -> float:
        """Per-asset (non-token) spend accumulated this run — image generation
        (slice 6). Added on top of the token cost for the budget gate + final usage."""
        return sum(getattr(self, "_aux_costs", []))

    async def _configure_image_gen(self) -> None:
        """Resolve the image model for this run and stash the per-run image config
        (plus the cost accumulator) in `self._extra`. The model is the session
        override (`extra['image_model_id']`) if set, else this text provider's catalog
        default — so image gen is **decoupled from the text provider** and can run
        cross-provider. The credential/base_url come from the chosen model's **home
        provider**, not the text provider. Presence of `extra['image_gen']` is what
        offers the `image_generate` tool; skips silently when the model is unknown or
        its home provider has no usable credential (the tool just isn't offered)."""
        import os

        from app.credentials import resolve_api_key
        from app.providers.image_models import get_image_model
        from app.providers.registry import get_provider_def

        imdef = get_image_model(self._extra.get("image_model_id"))
        if imdef is None and self._def.supports_image_gen:
            imdef = get_image_model(self._def.default_image_model_id)
        if imdef is None:
            return

        home = get_provider_def(imdef.provider)
        if home is None:
            return
        api_key = (
            "no-key" if home.auth_type == "none"
            else await resolve_api_key(home.name, home.env_key)
        )
        if not api_key:
            return
        self._extra["image_gen"] = {
            "api_key": api_key,
            "base_url": home.base_url or os.environ.get("OPENAI_BASE_URL") or None,
            "model": imdef.model,
            "cost_per_image": imdef.cost_per_image,
            "cost_per_mtok": imdef.cost_per_mtok,
            "response_format": imdef.response_format,
            "cost_accumulator": self._aux_costs,
        }

    async def run_stream(
        self,
        prompt: str,
        *,
        workdir: str,
        tools_enabled: bool = True,
        max_rounds: int = 10,
        budget_usd: float = 1.0,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        # Per-run dispatch context (e.g. the P-0046 code-exec execution policy);
        # threaded into tool listing (`_active_tool_schemas`) and dispatch.
        self._extra: dict[str, Any] = dict(extra or {})
        # Per-asset cost from non-token tools (image_generate); accumulated by the
        # tool via the shared list and folded into the run cost / budget gate.
        self._aux_costs: list[float] = []
        # Offer image generation when the run resolves to an image model: either the
        # session's explicit override (`image_model_id`, possibly cross-provider) or
        # this text provider's catalog default.
        if tools_enabled and (
            self._extra.get("image_model_id") or self._def.supports_image_gen
        ):
            await self._configure_image_gen()
        if self._def.kind == "anthropic":
            async for ev in self._run_anthropic(
                prompt, workdir=workdir, tools_enabled=tools_enabled,
                max_rounds=max_rounds, budget_usd=budget_usd,
            ):
                yield ev
        elif self._def.kind == "gemini":
            async for ev in self._run_gemini(
                prompt, workdir=workdir, tools_enabled=tools_enabled,
                max_rounds=max_rounds, budget_usd=budget_usd,
            ):
                yield ev
        else:
            async for ev in self._run_openai_compat(
                prompt, workdir=workdir, tools_enabled=tools_enabled,
                max_rounds=max_rounds, budget_usd=budget_usd,
            ):
                yield ev

    # ── OpenAI-compatible ─────────────────────────────────────────────────────

    async def _run_openai_compat(
        self, prompt: str, *, workdir: str, tools_enabled: bool,
        max_rounds: int, budget_usd: float,
    ) -> AsyncIterator[ExecEvent]:
        import os

        from openai import AsyncOpenAI

        from app.credentials import resolve_api_key

        api_key: str | None
        if self._def.auth_type == "none":
            # Unauthenticated local endpoint (Ollama, LM Studio, etc.).
            # The OpenAI SDK requires a non-empty string but the server ignores it.
            api_key = "no-key"
        else:
            api_key = await resolve_api_key(self._cred_provider, self._def.env_key)
        base_url = self._def.base_url or os.environ.get("OPENAI_BASE_URL") or None
        if not api_key:
            yield ExecEvent(
                kind=EventKind.error,
                message=(
                    f"no credentials for {self.name}: set {self._def.env_key} "
                    "or store a key via /api/credentials"
                ),
            )
            return

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        messages = [
            {"role": "system", "content": _base_system_prompt(self._extra)},
            {"role": "user", "content": prompt},
        ]
        tools = _active_tool_schemas(self._extra) if tools_enabled else []
        total_usage = Usage()
        full_text = ""

        yield ExecEvent(kind=EventKind.log, message=f"[{self.name}] starting (openai-compat)")
        yield ExecEvent(kind=EventKind.phase, phase="running")

        pending_synthesis = False
        for round_num in range(max_rounds):
            over_budget = total_usage.cost_usd + self._aux_cost() > budget_usd
            # Force a tool-free synthesis turn on the last round, when over budget, or
            # after a degenerate empty turn — so the loop always returns a complete
            # answer rather than exhausting mid-research or accepting an empty turn.
            force_answer = over_budget or round_num == max_rounds - 1 or pending_synthesis
            if over_budget:
                yield ExecEvent(
                    kind=EventKind.log,
                    message=f"[{self.name}] budget ${budget_usd:.4f} reached — synthesizing",
                )
            round_system = _base_system_prompt(self._extra) + (
                _SYNTHESIS_NUDGE if force_answer else "")
            messages[0] = {"role": "system", "content": round_system}
            # Deliver the synthesis instruction as a user turn too — the system nudge
            # alone is unreliable mid-conversation (see _SYNTHESIS_USER_MSG).
            if force_answer and len(messages) > 2:
                messages.append({"role": "user", "content": _SYNTHESIS_USER_MSG})
            # Keep the tools array present (history holds tool calls); forbid calls on
            # the synthesis turn via tool_choice "none" rather than dropping `tools`.
            create_kwargs: dict[str, Any] = dict(
                model=self._model or "gpt-4o-mini",
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
            )
            if tools:
                create_kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
                create_kwargs["tool_choice"] = "none" if force_answer else "auto"

            try:
                stream = await client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                err_msg = str(exc)
                if any(kw in err_msg.lower() for kw in ("rate", "limit", "quota", "429")):
                    yield ExecEvent(
                        kind=EventKind.error,
                        message=f"rate_limit_reached: {err_msg}",
                        data={"rate_limit": True},
                    )
                else:
                    yield ExecEvent(kind=EventKind.error, message=err_msg)
                return

            # Collect streamed response
            assistant_text = ""
            tool_calls_acc: dict[int, dict] = {}
            usage_delta = Usage()

            async for chunk in stream:
                if chunk.usage:
                    # OpenAI auto-caches stable prefixes (≥~1024 tok) and reports the
                    # cached count under prompt_tokens_details; unlike Anthropic, its
                    # prompt_tokens *includes* the cached portion, so split it out to
                    # avoid double-billing. No separate cache-write charge (automatic).
                    prompt_tok = chunk.usage.prompt_tokens or 0
                    cached = getattr(
                        getattr(chunk.usage, "prompt_tokens_details", None),
                        "cached_tokens", 0) or 0
                    usage_delta = Usage(
                        tokens_in=max(prompt_tok - cached, 0),
                        tokens_out=chunk.usage.completion_tokens or 0,
                        cache_read_tokens=cached,
                    )
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    assistant_text += delta.content
                    yield ExecEvent(kind=EventKind.token, text=delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": tc.id or "", "name": "", "args": ""}
                        if tc.function:
                            if tc.function.name:
                                tool_calls_acc[idx]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_acc[idx]["args"] += tc.function.arguments

            usage_delta.cost_usd = self._compute_cost(usage_delta)
            total_usage = total_usage + usage_delta
            # Keep the latest turn's text as the result (the synthesis turn's answer),
            # not a concatenation of every round's interstitial preamble.
            if assistant_text:
                full_text = assistant_text

            if force_answer:
                break
            if not tool_calls_acc:
                # Terminal turn. Keep its text if any; if empty, retry once as a
                # forced synthesis instead of returning nothing.
                if full_text:
                    break
                pending_synthesis = True
                continue

            # Run tools
            messages.append({"role": "assistant", "content": assistant_text,
                             "tool_calls": [
                                 {"id": v["id"], "type": "function",
                                  "function": {"name": v["name"], "arguments": v["args"]}}
                                 for v in tool_calls_acc.values()
                             ]})
            for v in tool_calls_acc.values():
                result = await self._call_tool(v["name"], v["args"], workdir=workdir)
                yield ExecEvent(kind=EventKind.tool, message=f"[{v['name']}] called",
                                data={"tool": v["name"], "result_chars": len(result)})
                messages.append({"role": "tool", "content": result, "tool_call_id": v["id"]})

        total_usage.cost_usd = self._compute_cost(total_usage) + self._aux_cost()
        exec_result = ExecResult(text=full_text, usage=total_usage, provider=self.name,
                                 model=self._model or "unknown")
        yield ExecEvent(kind=EventKind.result, message=f"[{self.name}] done",
                        data={"result": exec_result, "usage": total_usage.__dict__})

    # ── Anthropic ─────────────────────────────────────────────────────────────

    async def _run_anthropic(
        self, prompt: str, *, workdir: str, tools_enabled: bool,
        max_rounds: int, budget_usd: float,
    ) -> AsyncIterator[ExecEvent]:
        import anthropic

        from app.credentials import resolve_api_key

        api_key = await resolve_api_key(
            self._cred_provider, self._def.env_key or "ANTHROPIC_API_KEY"
        )
        if not api_key:
            yield ExecEvent(
                kind=EventKind.error,
                message=(
                    f"no credentials for {self.name}: "
                    f"set {self._def.env_key or 'ANTHROPIC_API_KEY'} "
                    "or store a key via /api/credentials"
                ),
            )
            return
        client = anthropic.AsyncAnthropic(api_key=api_key)

        messages = [{"role": "user", "content": prompt}]
        anth_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in _active_tool_schemas(self._extra)
        ] if tools_enabled else []
        # Prompt caching: mark the last tool as a cache breakpoint so the whole
        # stable prefix (system + tools) is cached and replayed at the cache-read
        # rate on every later round, instead of re-billing the full schema each
        # turn. Reduces cost *and* latency; the per-turn budget is kept honest by
        # the cache-read/write capture below. Requires a stable prefix ordering
        # (system + tools first, never reordered) — which this loop already keeps.
        if anth_tools:
            anth_tools[-1] = {**anth_tools[-1], "cache_control": {"type": "ephemeral"}}

        total_usage = Usage()
        full_text = ""

        yield ExecEvent(kind=EventKind.log, message=f"[{self.name}] starting (anthropic)")
        yield ExecEvent(kind=EventKind.phase, phase="running")

        pending_synthesis = False
        for round_num in range(max_rounds):
            over_budget = total_usage.cost_usd + self._aux_cost() > budget_usd
            # Force a tool-free synthesis turn when: we've hit the last permitted round,
            # we're over budget, or a previous turn ended without producing any text
            # (a degenerate empty turn). Otherwise the loop can exhaust on a tool_use
            # turn — or accept an empty turn — and return nothing / an interstitial line.
            force_answer = over_budget or round_num == max_rounds - 1 or pending_synthesis
            if over_budget:
                yield ExecEvent(
                    kind=EventKind.log,
                    message=f"[{self.name}] budget ${budget_usd:.4f} reached — synthesizing",
                )

            # Forcing the synthesis turn: OMIT the tools array entirely (not tools=None,
            # which the API rejects once history holds tool_use blocks) AND deliver the
            # synthesis instruction as a user turn — the system-suffix nudge alone
            # often gets an empty reply mid-conversation (see _SYNTHESIS_USER_MSG).
            if force_answer and len(messages) > 1:
                messages.append({"role": "user", "content": _SYNTHESIS_USER_MSG})
            system_text = _base_system_prompt(self._extra) + (
                _SYNTHESIS_NUDGE if force_answer else "")
            stream_kwargs: dict[str, Any] = dict(
                model=self._model or "claude-opus-4-5",
                max_tokens=8192,
                # System as a cacheable block: the base prompt is identical every
                # round (the nudge only appends on the final forced turn), so it
                # caches alongside the tools prefix above.
                system=[{
                    "type": "text", "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=messages,
            )
            if anth_tools and not force_answer:
                stream_kwargs["tools"] = anth_tools

            try:
                async with client.messages.stream(**stream_kwargs) as stream:
                    async for event in stream:
                        if hasattr(event, "type"):
                            if event.type == "content_block_delta":
                                if hasattr(event.delta, "text"):
                                    yield ExecEvent(kind=EventKind.token, text=event.delta.text)

                    final_msg = await stream.get_final_message()
                    # Anthropic reports cached input separately: `input_tokens` is
                    # the uncached portion, with cache reads/writes alongside — so
                    # the three sum to the real prompt and each bills at its rate.
                    in_tok = final_msg.usage.input_tokens
                    out_tok = final_msg.usage.output_tokens
                    cache_read = getattr(final_msg.usage, "cache_read_input_tokens", 0) or 0
                    cache_write = getattr(
                        final_msg.usage, "cache_creation_input_tokens", 0) or 0
                    delta = Usage(tokens_in=in_tok, tokens_out=out_tok,
                                  cache_read_tokens=cache_read, cache_write_tokens=cache_write)
                    delta.cost_usd = self._compute_cost(delta)
                    total_usage = total_usage + delta

                    tool_uses = [b for b in final_msg.content if b.type == "tool_use"]
                    # Authoritative text for this turn (may be multiple text blocks);
                    # the latest turn's text is the result we keep.
                    text_blocks = [b.text for b in final_msg.content if b.type == "text"]
                    if text_blocks:
                        full_text = "".join(text_blocks)

                    if force_answer:
                        break
                    if final_msg.stop_reason != "tool_use" or not tool_uses:
                        # Terminal turn. If it produced text, that's the answer. If it
                        # was empty, retry once as a forced synthesis instead of
                        # returning nothing.
                        if full_text:
                            break
                        pending_synthesis = True
                        continue

                    # Run tool calls
                    messages.append({"role": "assistant", "content": final_msg.content})
                    tool_results = []
                    for tu in tool_uses:
                        result = await self._call_tool(
                            tu.name, json.dumps(tu.input), workdir=workdir
                        )
                        yield ExecEvent(kind=EventKind.tool, message=f"[{tu.name}] called",
                                        data={"tool": tu.name, "result_chars": len(result)})
                        tool_results.append(
                            {"type": "tool_result", "tool_use_id": tu.id, "content": result}
                        )
                    messages.append({"role": "user", "content": tool_results})

            except Exception as exc:
                err = str(exc)
                if any(kw in err.lower() for kw in ("rate", "limit", "quota", "429", "overloaded")):
                    yield ExecEvent(kind=EventKind.error, message=f"rate_limit_reached: {err}",
                                    data={"rate_limit": True})
                else:
                    yield ExecEvent(kind=EventKind.error, message=err)
                return

        total_usage.cost_usd += self._aux_cost()
        exec_result = ExecResult(text=full_text, usage=total_usage, provider=self.name,
                                 model=self._model or "unknown")
        yield ExecEvent(kind=EventKind.result, message=f"[{self.name}] done",
                        data={"result": exec_result, "usage": total_usage.__dict__})

    # ── Gemini (native google-genai) ──────────────────────────────────────────

    async def _run_gemini(
        self, prompt: str, *, workdir: str, tools_enabled: bool,
        max_rounds: int, budget_usd: float,
    ) -> AsyncIterator[ExecEvent]:
        # Native path (D-0034 / P-0043). The OpenAI-compat shim drops Gemini's
        # `thought_signature`, which thinking models require replayed on every
        # later turn — so multi-step tool use 400s on the second tool round. Here
        # we replay the model's `Content` parts **verbatim** (signatures intact),
        # which is the whole reason this path exists; do not "clean up" or merge
        # the model parts before sending them back.
        from google import genai
        from google.genai import types

        from app.credentials import resolve_api_key

        api_key = await resolve_api_key(
            self._cred_provider, self._def.env_key or "GEMINI_API_KEY"
        )
        if not api_key:
            yield ExecEvent(
                kind=EventKind.error,
                message=(
                    f"no credentials for {self.name}: "
                    f"set {self._def.env_key or 'GEMINI_API_KEY'} "
                    "or store a key via /api/credentials"
                ),
            )
            return

        client = genai.Client(api_key=api_key)
        tools = None
        if tools_enabled:
            tools = [
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        parameters_json_schema=t["parameters"],
                    )
                    for t in _active_tool_schemas(self._extra)
                ])
            ]
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=prompt)])
        ]
        total_usage = Usage()
        full_text = ""

        yield ExecEvent(kind=EventKind.log, message=f"[{self.name}] starting (gemini)")
        yield ExecEvent(kind=EventKind.phase, phase="running")

        pending_synthesis = False
        for round_num in range(max_rounds):
            over_budget = total_usage.cost_usd + self._aux_cost() > budget_usd
            # Force a tool-free synthesis turn on the last round, when over budget, or
            # after a degenerate empty turn — so the loop always returns a complete
            # answer rather than exhausting mid-research or accepting an empty turn.
            force_answer = over_budget or round_num == max_rounds - 1 or pending_synthesis
            if over_budget:
                yield ExecEvent(
                    kind=EventKind.log,
                    message=f"[{self.name}] budget ${budget_usd:.4f} reached — synthesizing",
                )
            # Deliver the synthesis instruction as a user turn too — the system nudge
            # alone is unreliable mid-conversation (see _SYNTHESIS_USER_MSG).
            if force_answer and len(contents) > 1:
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=_SYNTHESIS_USER_MSG)])
                )
            # Keep the tools declared (history holds function calls); on the synthesis
            # turn forbid new calls via FunctionCallingConfig mode=NONE rather than
            # dropping `tools`.
            tool_config = None
            if force_answer and tools:
                tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.NONE
                    )
                )
            config = types.GenerateContentConfig(
                system_instruction=_base_system_prompt(self._extra) + (
                    _SYNTHESIS_NUDGE if force_answer else ""),
                tools=tools,
                tool_config=tool_config,
            )

            model_parts: list[types.Part] = []
            round_text = ""
            last_usage = None
            try:
                stream = await client.aio.models.generate_content_stream(
                    model=self._model or "gemini-2.5-flash",
                    contents=contents,
                    config=config,
                )
                async for chunk in stream:
                    if chunk.usage_metadata:
                        last_usage = chunk.usage_metadata
                    cand = chunk.candidates[0] if chunk.candidates else None
                    if not cand or not cand.content or not cand.content.parts:
                        continue
                    for part in cand.content.parts:
                        # Replay every part verbatim (signatures live here). Only
                        # surface non-thought answer text as tokens to the user.
                        model_parts.append(part)
                        if part.text and not part.thought:
                            round_text += part.text
                            yield ExecEvent(kind=EventKind.token, text=part.text)
            except Exception as exc:
                err = str(exc)
                _rl = ("rate", "limit", "quota", "429", "resource_exhausted")
                if any(kw in err.lower() for kw in _rl):
                    yield ExecEvent(kind=EventKind.error, message=f"rate_limit_reached: {err}",
                                    data={"rate_limit": True})
                else:
                    yield ExecEvent(kind=EventKind.error, message=err)
                return

            if last_usage:
                tokens_out = last_usage.candidates_token_count or 0
                if last_usage.thoughts_token_count:
                    tokens_out += last_usage.thoughts_token_count
                # Gemini's prompt_token_count includes any implicitly-cached content
                # (reported as cached_content_token_count); split it out so the cached
                # portion bills at the cache-read rate, not full input rate.
                prompt_tok = last_usage.prompt_token_count or 0
                cached = getattr(last_usage, "cached_content_token_count", 0) or 0
                delta = Usage(tokens_in=max(prompt_tok - cached, 0),
                              tokens_out=tokens_out, cache_read_tokens=cached)
                delta.cost_usd = self._compute_cost(delta)
                total_usage = total_usage + delta

            # Keep the latest turn's text as the result (the synthesis answer).
            if round_text:
                full_text = round_text

            fcalls = [p.function_call for p in model_parts if p.function_call]
            if force_answer:
                break
            if not fcalls:
                # Terminal turn. Keep its text if any; if empty, retry once as a
                # forced synthesis instead of returning nothing.
                if full_text:
                    break
                pending_synthesis = True
                continue

            # Replay the model turn (incl. thought_signature) then the tool results.
            contents.append(types.Content(role="model", parts=model_parts))
            resp_parts = []
            for fc in fcalls:
                result = await self._call_tool(
                    fc.name, json.dumps(dict(fc.args or {})), workdir=workdir
                )
                yield ExecEvent(kind=EventKind.tool, message=f"[{fc.name}] called",
                                data={"tool": fc.name, "result_chars": len(result)})
                resp_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response={"result": result}
                    )
                )
            contents.append(types.Content(role="user", parts=resp_parts))

        total_usage.cost_usd = self._compute_cost(total_usage) + self._aux_cost()
        exec_result = ExecResult(text=full_text, usage=total_usage, provider=self.name,
                                 model=self._model or "unknown")
        yield ExecEvent(kind=EventKind.result, message=f"[{self.name}] done",
                        data={"result": exec_result, "usage": total_usage.__dict__})

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    async def _call_tool(self, name: str, args_json: str, *, workdir: str) -> str:
        return await get_tool_registry().call(
            name, args_json, workdir=workdir, context=getattr(self, "_extra", None)
        )
