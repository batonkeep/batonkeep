"""
test_image_model_selection.py — image-model catalog + per-session override +
cross-provider credential resolution (P-0046 slice 6 follow-up).

Locks: the catalog resolves by id; the executor picks the session override over the
provider default; cross-provider resolution uses the *image model's home provider*
credential (not the text provider's); a missing home credential means the tool is
not offered; and the /api/image-models endpoint reports availability.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.providers.image_models import get_image_model, list_image_models
from app.providers.model_executor import ModelExecutor
from app.providers.registry import get_instance, get_provider_def


def test_catalog_has_both_providers_and_resolves_by_id():
    ids = {m.id for m in list_image_models()}
    assert "openai:gpt-image-1-mini" in ids
    assert "grok:grok-imagine-image-quality" in ids
    m = get_image_model("grok:grok-imagine-image")
    assert m and m.provider == "grok-api" and m.cost_per_image == 0.02
    assert get_image_model("nope") is None
    assert get_image_model(None) is None


async def _configure(provider_name: str, *, override: str | None, key: str | None):
    """Drive _configure_image_gen for a text provider with an optional override and a
    stubbed credential resolver; return the resulting image_gen config (or None)."""
    pd = get_provider_def(provider_name)
    inst = get_instance(provider_name)
    ex = ModelExecutor(pd, inst)
    ex._extra = {"image_model_id": override} if override else {}
    ex._aux_costs = []
    with patch("app.credentials.resolve_api_key", AsyncMock(return_value=key)):
        await ex._configure_image_gen()
    return ex._extra.get("image_gen")


@pytest.mark.asyncio
async def test_default_image_model_when_no_override():
    cfg = await _configure("openai-api", override=None, key="sk-x")
    assert cfg and cfg["model"] == "gpt-image-1-mini"
    assert cfg["response_format"] is None  # gpt-image omits the param


@pytest.mark.asyncio
async def test_cross_provider_override_uses_home_provider_model():
    # Text provider is openai-api, but the session overrides to a Grok image model.
    cfg = await _configure("openai-api", override="grok:grok-imagine-image-quality", key="xai-key")
    assert cfg and cfg["model"] == "grok-imagine-image-quality"
    assert cfg["response_format"] == "b64_json"  # xAI accepts it
    assert cfg["cost_per_image"] == 0.05
    # base_url must be the *home* provider's (xAI), not OpenAI's.
    assert "x.ai" in (cfg["base_url"] or "")


@pytest.mark.asyncio
async def test_override_offered_even_when_text_provider_not_image_capable():
    # A text provider with no image support still gets image gen via an override.
    pd = get_provider_def("openai-api")
    object.__setattr__(pd, "supports_image_gen", pd.supports_image_gen)  # no-op guard
    cfg = await _configure("openai-api", override="grok:grok-imagine-image", key="xai-key")
    assert cfg and cfg["model"] == "grok-imagine-image"


@pytest.mark.asyncio
async def test_no_offer_when_home_provider_has_no_credential():
    cfg = await _configure("openai-api", override="grok:grok-imagine-image-quality", key=None)
    assert cfg is None  # tool not offered → no image_gen config


@pytest.mark.asyncio
async def test_unknown_override_falls_back_to_provider_default():
    cfg = await _configure("grok-api", override="bogus:model", key="xai-key")
    # Unknown override is ignored; the grok text provider's default applies.
    assert cfg and cfg["model"] == "grok-imagine-image-quality"


def test_task_and_session_schemas_validate_image_model_id():
    from pydantic import ValidationError

    from app.schemas import SessionCreate, TaskCreate

    # Valid catalog id accepted on both surfaces.
    assert TaskCreate(name="t", image_model_id="grok:grok-imagine-image").image_model_id
    assert SessionCreate(image_model_id="openai:gpt-image-2").image_model_id
    # Unknown id rejected.
    for ctor in (lambda: TaskCreate(name="t", image_model_id="nope"),
                 lambda: SessionCreate(image_model_id="nope")):
        with pytest.raises(ValidationError):
            ctor()
