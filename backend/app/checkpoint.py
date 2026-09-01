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


def serialize_messages(messages: list[Any]) -> list[Any]:
    """Return a JSON-safe copy of a provider-native message list.

    Raises on anything it cannot represent — deliberately. A checkpoint that silently
    dropped a `thought_signature` would resume into an opaque provider error much later;
    failing here means the caller falls back to waiting in-process and nothing is lost.
    """
    import json

    return json.loads(json.dumps(messages, default=_coerce))
