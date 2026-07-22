"""Structured-planner protocol for the plan/CLI lane (P-0082).

The planner lane's semantics live in `providers/tools/planner_tools.py` as pure
functions with JSON Schema definitions. The API lane reaches them through native
tool-calling. The plan/CLI lane could not reach them **at all**: `CLIExecutor`
shells out to a binary and never consults the tool registry, so `extra["planning"]`
was discarded and six consecutive planner runs on the live instance produced zero
structural output while five recorded `succeeded`.

This module is the second transport to the *same* functions — not a second
implementation of planning. Two properties keep the lanes from drifting:

1. **The protocol text is generated from the same schemas the API lane offers.**
   Adding or changing a planner tool changes both transports at once; there is no
   second copy of the contract to forget.
2. **Parsed calls dispatch through the same `PlannerToolProvider.call`.** All the
   validation the tools already do — ids filtered against the project's real work
   items, owner scoping, proposer-only status — applies identically.

What this transport does *not* give the CLI lane is interaction parity: the API
lane gets tool results back and can refine over several rounds, while a one-shot
text block cannot. That gap is deliberate in V1 and stated rather than hidden.

Compliance is probabilistic where native tool-calling is enforced, so parsing is
deliberately tolerant of the things models actually do — a fenced block among
prose, a bare array, one object instead of a list, a trailing comma — and refuses
everything else rather than guessing.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: The fence the model is asked to emit. Distinctive enough that ordinary prose
#: about planning cannot collide with it.
BLOCK_TAG = "batonkeep-plan"

_FENCE_RE = re.compile(
    r"```(?:\s*" + re.escape(BLOCK_TAG) + r")\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
#: Trailing commas are the single most common hand-written-JSON error models make.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def protocol_instructions(schemas: list[dict]) -> str:
    """The contract text for a planning turn, generated from the tool schemas.

    Generated — never hand-written — so the CLI lane cannot describe a tool the
    API lane no longer offers, or miss one it gained.
    """
    if not schemas:
        return ""
    lines = [
        "## Recording your plan (required)",
        "",
        "Your prose is read by a person, but it is **not** how your plan is recorded.",
        "To record anything durable you MUST emit one fenced block, exactly once, at",
        f"the very end of your reply:",
        "",
        f"```{BLOCK_TAG}",
        '[{"tool": "<name>", "args": { … }}]',
        "```",
        "",
        "It is a JSON array of calls, applied in order. Available calls:",
        "",
    ]
    for s in schemas:
        params = s.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        lines.append(f"- **`{s['name']}`** — {s.get('description', '').strip()}")
        for key, spec in props.items():
            flag = "required" if key in required else "optional"
            desc = (spec.get("description") or "").strip()
            lines.append(f"    - `{key}` ({spec.get('type', 'any')}, {flag}) {desc}")
    lines += [
        "",
        "Rules:",
        "- Emit the block even when your conclusion is that **nothing needs doing** —",
        "  record that finding with a call rather than only saying it in prose. A turn",
        "  that emits no block is recorded as having proposed nothing at all.",
        "- Use only the calls listed above, with exactly those argument names.",
        "- One block per reply. No commentary inside it.",
    ]
    return "\n".join(lines)


def extract_calls(text: str) -> tuple[list[dict], str | None]:
    """Parse a reply into `(calls, error)`. `calls` is `[{"tool", "args"}, …]`.

    `error` is a short reason when a block was present but unusable — kept
    distinct from "no block at all", because a malformed block is a model that
    tried and a missing one is a model that did not.
    """
    if not text:
        return [], None
    matches = _FENCE_RE.findall(text)
    if not matches:
        return [], None

    # Last block wins: models that revise mid-reply leave the corrected one last.
    raw = matches[-1].strip()
    if len(matches) > 1:
        logger.info("[planner-protocol] %d blocks emitted; using the last", len(matches))

    payload, err = _loads_tolerant(raw)
    if err:
        return [], err

    if isinstance(payload, dict):
        payload = [payload]          # one call, unwrapped
    if not isinstance(payload, list):
        return [], "plan block is not a JSON array of calls"

    calls: list[dict] = []
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            return [], f"call {i} is not an object"
        name = item.get("tool") or item.get("name")
        if not isinstance(name, str) or not name:
            return [], f"call {i} has no tool name"
        args = item.get("args", item.get("arguments", {}))
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return [], f"call {i} ({name}) has non-object args"
        calls.append({"tool": name, "args": args})
    return calls, None


def _loads_tolerant(raw: str) -> tuple[Any, str | None]:
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_TRAILING_COMMA_RE.sub(r"\1", raw)), None
    except json.JSONDecodeError as exc:
        return None, f"plan block is not valid JSON ({exc.msg} at line {exc.lineno})"


def strip_block(text: str) -> str:
    """The reply with the protocol block removed — what a human should read.

    The prose is kept and shown; it is often the most useful thing a planning turn
    produces. It is simply not the record.
    """
    return _FENCE_RE.sub("", text or "").strip()
