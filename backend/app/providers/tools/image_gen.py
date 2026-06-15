"""
providers/tools/image_gen.py — capability-gated image generation (P-0046 slice 6 / P-0037).

The API path reaches CLI-lane multimodal parity by *generating* media and capturing
it into the session workspace as a normal artifact (D-0017), so it flows through the
existing MIME-aware preview pane (D-0028) + Files tab (D-0031) — no new display surface.

This tool is only *offered* to the model when the active provider declares
`supports_image_gen` (registry); gating keys on the model's capability, not the
provider kind. V1 wires xAI/Grok's OpenAI-shaped images endpoint. The executor
injects the per-run config (base_url / model / credential) + a cost accumulator via
the registry dispatch `context` — the tool itself holds no provider knowledge.

Cost posture (founder, 2026-06-15): images bill *per-asset*, not per-token, so the
per-image price is metered into the run cost via the `image_cost` accumulator and
counts against the session/daily budget like any other spend (P-0009 #2).
"""
from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

TOOL_SCHEMA = {
    "name": "image_generate",
    "description": (
        "Generate an image from a text prompt and save it into the workspace. "
        "Returns the saved file path; the image renders in the preview pane and "
        "Files tab. Use for diagrams, illustrations, mockups, or any visual asset "
        "the user asks you to create."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Description of the image to generate.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Relative filename within the workspace (e.g. 'logo.png'). "
                    "Defaults to assets/generated-<n>.png if omitted."
                ),
            },
        },
        "required": ["prompt"],
    },
}


def _safe_target(workdir: str, filename: str) -> str | None:
    """Resolve `filename` under `workdir`, refusing traversal (mirrors file_write)."""
    safe = os.path.normpath(filename).lstrip("/")
    if ".." in safe.split(os.sep):
        return None
    return os.path.join(workdir, safe)


async def run(
    prompt: str,
    filename: str | None = None,
    *,
    workdir: str,
    config: dict | None = None,
) -> str:
    """Generate one image via the configured provider's images endpoint and write it
    into the workspace. `config` (from the executor) carries `api_key`, `base_url`,
    `model`, `cost_per_image`, and a mutable `cost_accumulator` list the tool appends
    the per-asset cost to so the run's budget gate sees it."""
    cfg = config or {}
    api_key = cfg.get("api_key")
    model = cfg.get("model")
    if not api_key or not model:
        return "[image_generate error] no image-capable provider configured for this run"

    # Default the destination into assets/ so it groups with other media (D-0029).
    if not filename:
        filename = f"assets/generated-{cfg.get('seq', 1)}.png"
    elif "." not in os.path.basename(filename):
        filename = f"{filename}.png"
    target = _safe_target(workdir, filename)
    if target is None:
        return "[image_generate error] path traversal rejected"

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=cfg.get("base_url") or None)
    try:
        resp = await client.images.generate(
            model=model, prompt=prompt, response_format="b64_json", n=1,
        )
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in ("rate", "limit", "quota", "429")):
            return f"[image_generate error] rate_limit_reached: {msg}"
        return f"[image_generate error] {msg}"

    data = resp.data[0] if resp.data else None
    b64 = getattr(data, "b64_json", None) if data else None
    if not b64:
        return "[image_generate error] provider returned no image data"

    os.makedirs(os.path.dirname(target) or workdir, exist_ok=True)
    with open(target, "wb") as f:
        f.write(base64.b64decode(b64))

    # Meter the per-asset cost into the run so it counts against budget (P-0009 #2).
    cost = float(cfg.get("cost_per_image", 0.0) or 0.0)
    acc = cfg.get("cost_accumulator")
    if isinstance(acc, list):
        acc.append(cost)

    rel = os.path.relpath(target, workdir)
    logger.info("image_generate wrote %s (model=%s, $%.4f)", rel, model, cost)
    return f"[image_generate] saved {rel} (${cost:.4f})"
