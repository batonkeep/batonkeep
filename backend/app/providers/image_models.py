"""
providers/image_models.py — the curated image-generation model catalog (P-0046
slice 6 follow-up: image-model selection + cross-provider override).

Image generation is decoupled from the session's *text* provider: a build session
running text on `openai-api` can render images with a Grok image model (or any
catalog entry the operator picks), as long as the chosen model's **home provider**
has a usable credential. The catalog is the single source of truth for each image
model's home provider (credential/base_url source), API model string, billing
shape, and `response_format` handling.

Selection flow:
  - **Default** — a session inherits its text provider's `default_image_model_id`
    (e.g. `openai-api` → `openai:gpt-image-1-mini`).
  - **Override** — `Session.image_model_id` names any catalog entry, including one
    whose home provider differs from the text provider (cross-provider).
The executor (`model_executor._configure_image_gen`) resolves the chosen entry,
resolves the *home provider's* credential, and builds the per-run `image_gen`
config the `image_generate` tool dispatches against.

Billing (P-0009 #2): two shapes — flat `cost_per_image` (xAI/Grok) and per-token
`cost_per_mtok` against the response `usage` block (OpenAI `gpt-image-*`). The tool
meters per-token when usage is present, else the flat rate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImageModelDef:
    id: str                              # catalog id, e.g. "openai:gpt-image-2"
    label: str                           # UI label
    provider: str                        # home provider name (credential/base_url source)
    model: str                           # API model string sent to the images endpoint
    cost_per_image: float = 0.0          # flat per-asset cost (xAI/Grok)
    cost_per_mtok: float = 0.0           # per-Mtok image-output cost (OpenAI gpt-image-*)
    # OpenAI `gpt-image-*` rejects `response_format` (returns b64 always); xAI accepts
    # `b64_json`. None = omit the param.
    response_format: str | None = "b64_json"


# Curated, Batonkeep-vetted image models. Prices are the providers' published rates;
# confirm against the enabled plan. (OpenAI gpt-image bills per-token image output.)
_IMAGE_MODELS: list[ImageModelDef] = [
    ImageModelDef(
        id="openai:gpt-image-2", label="OpenAI · gpt-image-2",
        provider="openai-api", model="gpt-image-2",
        cost_per_mtok=30.0, response_format=None,
    ),
    ImageModelDef(
        id="openai:gpt-image-1.5", label="OpenAI · gpt-image-1.5",
        provider="openai-api", model="gpt-image-1.5",
        cost_per_mtok=32.0, response_format=None,
    ),
    ImageModelDef(
        id="openai:gpt-image-1-mini", label="OpenAI · gpt-image-1-mini",
        provider="openai-api", model="gpt-image-1-mini",
        cost_per_mtok=8.0, cost_per_image=0.01, response_format=None,
    ),
    ImageModelDef(
        id="grok:grok-imagine-image-quality", label="Grok · image (quality)",
        provider="grok-api", model="grok-imagine-image-quality",
        cost_per_image=0.05, response_format="b64_json",
    ),
    ImageModelDef(
        id="grok:grok-imagine-image", label="Grok · image",
        provider="grok-api", model="grok-imagine-image",
        cost_per_image=0.02, response_format="b64_json",
    ),
]

_BY_ID: dict[str, ImageModelDef] = {m.id: m for m in _IMAGE_MODELS}


def list_image_models() -> list[ImageModelDef]:
    """All catalog entries (availability — whether the home provider has a credential
    — is resolved by the API layer, which can do the async credential lookup)."""
    return list(_IMAGE_MODELS)


def get_image_model(model_id: str | None) -> ImageModelDef | None:
    """Resolve a catalog entry by id; None for unknown/empty ids."""
    if not model_id:
        return None
    return _BY_ID.get(model_id)
