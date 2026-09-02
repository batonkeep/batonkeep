"""app/checkpoint.py — parked-run conversation checkpoints (P-0106 / D-0069).

When an unattended run parks on a human decision it stops mid-conversation. Storing that
conversation is what lets the decision be acted on **after a restart**, instead of the run
dying with the process and the operator's verdict applying to nothing.

**What is stored, and why it is provider-native.** The `messages` list is kept in the exact
shape its SDK produced. Normalizing to a neutral form would drop Gemini's
`thought_signature` — which thinking models require replayed verbatim on every later turn,
or multi-step tool use `400`s on the second tool round (the whole reason `_run_gemini`
exists, D-0034/P-0043) — and Anthropic's cache-breakpoint placement. A neutral format would
look tidier and be wrong.

**The fence is what makes that safe.** Storing opaque provider blobs across a process
boundary is fine; storing them across an *SDK upgrade* is not, and this repo's images
install SDKs at build time, so a rebuild can move them without any source change (the same
drift that moved three CLI lanes on one commit). Every checkpoint therefore records the
provider kind, the model, and the installed SDK version, and `verify_fence` **refuses to
resume across a mismatch** — failing honestly rather than replaying a stale signature into
an opaque provider error. This is the D-0058 A2 version-probe pattern applied to state at
rest.
"""
from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1

#: The distribution whose version is pinned into the fence, per provider kind. These are
#: the SDKs whose serialized message shapes we are storing.
_SDK_DIST = {
    "anthropic": "anthropic",
    "openai_compat": "openai",
    # [[D-0074]]: `/v1/responses` is a fourth request shape and therefore a fourth
    # checkpoint path — same SDK, but a different conversation representation, so a
    # checkpoint written by one loop must never be replayed by the other. `_resume_state`
    # keys on this value, so a mismatched path simply does not resume.
    "openai_responses": "openai",
    "gemini": "google-genai",
}


def _sdk_version(path: str) -> str:
    dist = _SDK_DIST.get(path)
    if not dist:
        return "unknown"
    try:
        return metadata.version(dist)
    except Exception:  # pragma: no cover - absent dist in a stripped env
        return "unknown"


def build(
    *,
    path: str,
    provider: str,
    model: str | None,
    messages: list[Any],
    usage: dict[str, Any],
    round_num: int,
    tool_call: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a checkpoint. `path` is the executor loop that produced `messages`."""
    return {
        "v": CHECKPOINT_VERSION,
        "fence": {
            "path": path,
            "provider": provider,
            "model": model,
            "sdk": _sdk_version(path),
        },
        "messages": messages,
        "usage": usage,
        "round": round_num,
        "tool_call": tool_call,
    }


def verify_fence(cp: dict[str, Any] | None, *, provider: str, model: str | None) -> str | None:
    """Return None when the checkpoint is safe to resume, else why it is not.

    Deliberately strict. A checkpoint that *might* replay is worse than one that refuses:
    a mismatched signature surfaces as an opaque provider `400` mid-run, long after the
    cause. Refusing gives the operator a sentence they can act on.
    """
    if not cp:
        return "no checkpoint was stored for this approval"
    if cp.get("v") != CHECKPOINT_VERSION:
        return (
            f"checkpoint format v{cp.get('v')} was written by a different build "
            f"(this build reads v{CHECKPOINT_VERSION})"
        )
    fence = cp.get("fence") or {}
    if fence.get("provider") != provider:
        return (
            f"checkpoint was written for provider '{fence.get('provider')}', "
            f"but the run would resume on '{provider}'"
        )
    if fence.get("model") != model:
        return (
            f"checkpoint was written for model '{fence.get('model')}', "
            f"but the run would resume on '{model}'"
        )
    current = _sdk_version(fence.get("path", ""))
    if fence.get("sdk") != current:
        return (
            f"checkpoint was written with {fence.get('path')} SDK {fence.get('sdk')}, "
            f"but this build has {current} — provider-native message state "
            "(e.g. Gemini thought signatures) is not portable across SDK versions"
        )
    return None


class ParkRequested(Exception):
    """Raised by an approver that has stored a checkpoint and wants the run to stop.

    Control-flow signal, not an error. It unwinds the executor's tool dispatch so the loop
    can end cleanly and the process is freed — which is the entire point: an in-process
    ``await`` cannot survive a restart, so the run must *stop* rather than *wait*.

    An approver only raises this when the checkpoint was actually stored. If checkpointing
    is unavailable (an unserializable message shape, a DB failure), it falls back to
    waiting in-process — the pre-P-0106 behaviour — so this can only ever be an
    improvement over waiting, never a new way to lose a run.
    """

    def __init__(self, request_id: str) -> None:
        super().__init__(f"run parked awaiting approval {request_id}")
        self.request_id = request_id


def _coerce(obj: Any) -> Any:
    """JSON-encode SDK objects by asking them for their own dict form.

    Provider SDKs hand back pydantic models (Anthropic content blocks) or protobuf-backed
    wrappers (`google-genai` `Content`). Each exposes a dict form; we try them in order
    rather than special-casing per provider, so a fourth backend needs no change here.
    """
    for attr in ("model_dump", "to_json_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: S112 - try the next shape
                continue
    raise TypeError(f"cannot serialize {type(obj).__name__} into a checkpoint")


#: Keys whose value is a **tool call's own payload**, never provider metadata:
#: Anthropic `tool_use.input`, Gemini `function_call.args` / `function_response.response`,
#: OpenAI `function.arguments`. `_drop_null_fields` does not descend into these — see there.
_PAYLOAD_KEYS = frozenset({"input", "args", "response", "arguments"})


def _drop_null_fields(obj: Any) -> Any:
    """Strip null-valued keys from provider message dicts, leaving tool payloads alone.

    **Why this is here at all.** A checkpoint is only useful if the provider accepts it
    back, and `model_dump()` emits *output-only* fields that the same provider refuses on
    input. Found on Runtime B by `DRILL-PARK-R6`: an Anthropic assistant turn that narrates
    before calling its tool carries a `text` block whose dump includes
    `"citations": null, "parsed_output": null`, and replaying it is a hard 400 —

        messages.1.content.0.text.parsed_output: Extra inputs are not permitted

    The live path never sees this, because the loop appends the SDK's own objects and the
    SDK strips them. Only a **resumed** run replays these dicts. So Gate B's
    restart-survival guarantee was broken on the Anthropic lane for any run where the model
    said something before calling the tool — most of them. `DRILL-PARK-R3a` passed only
    because that turn happened to be a bare `tool_use`.

    **Why here rather than in each loop.** [[D-0074]] fixed the same defect on the
    Responses lane by normalizing where the loop appends, so live and resumed rounds send
    identical bytes. That is the better shape when it is available — and on the Anthropic
    lane it is not: the loop deliberately re-sends the SDK's objects (cache-control
    breakpoints are managed around them), so normalizing at the append site would change
    the **live** request path to fix a **resume-only** defect. Doing it in the checkpoint
    instead gives a stronger and simpler guarantee anyway: *what this function returns is
    valid provider input, by construction, for every lane.*

    **The carve-out is the load-bearing part.** A tool call's arguments are **the action the
    operator approved**. Stripping a null from inside them would resume a *different* call
    than the one adjudicated — the exact substitution D-0069 refuses, arriving by the back
    door. So `_PAYLOAD_KEYS` values are kept verbatim, nulls included.
    """
    if isinstance(obj, dict):
        return {
            k: (v if k in _PAYLOAD_KEYS else _drop_null_fields(v))
            for k, v in obj.items()
            if v is not None
        }
    if isinstance(obj, list):
        return [_drop_null_fields(v) for v in obj]
    return obj


def serialize_messages(messages: list[Any]) -> list[Any]:
    """Return a JSON-safe copy of a provider-native message list, in the shape the
    provider will accept back.

    Raises on anything it cannot represent — deliberately. A checkpoint that silently
    dropped a `thought_signature` would resume into an opaque provider error much later;
    failing here means the caller falls back to waiting in-process and nothing is lost.
    """
    import json

    return _drop_null_fields(json.loads(json.dumps(messages, default=_coerce)))
