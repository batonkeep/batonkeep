"""
test_model_catalog.py — P-0049 structured API model catalog.

Covers the baked-in derivation, overlay apply, per-capability preferred resolution,
write-through persistence (structure + pricing), and the effective_model integration.
"""
from __future__ import annotations

import json

import pytest

from app.providers import model_catalog, model_pricing


@pytest.fixture
def tmp_overlays(tmp_path):
    """Point both overlay files at tmp paths and rebuild from baked defaults. Restores
    the original paths *before* the final reload so edits don't leak into other tests
    (module-level `_CATALOG`/`_PRICES` are global)."""
    orig_cat, orig_price = model_catalog._CATALOG_PATH, model_pricing._PRICING_PATH
    cat = tmp_path / "model-catalog.json"
    price = tmp_path / "model-pricing.json"
    model_catalog._CATALOG_PATH = str(cat)
    model_pricing._PRICING_PATH = str(price)
    model_pricing.reload()
    model_catalog.reload()
    try:
        yield cat, price
    finally:
        model_catalog._CATALOG_PATH = orig_cat
        model_pricing._PRICING_PATH = orig_price
        model_pricing.reload()
        model_catalog.reload()


def test_baked_buckets_models_by_provider(tmp_overlays):
    claude = {m.id for m in model_catalog.provider_models("claude-api")}
    assert "claude-opus-4-8" in claude
    assert all(m.id.startswith("claude") for m in model_catalog.provider_models("claude-api"))
    # Default preferred seeds from the registry default model.
    assert model_catalog.preferred("claude-api", "default") == "claude-opus-4-5"
    # Unset capability falls back to default.
    assert model_catalog.preferred("claude-api", "coding") == "claude-opus-4-5"


def test_set_preferred_persists_and_reloads(tmp_overlays):
    cat, _ = tmp_overlays
    model_catalog.set_preferred("claude-api", "coding", "claude-opus-4-8")
    assert model_catalog.preferred("claude-api", "coding") == "claude-opus-4-8"
    # Persisted to the overlay file…
    saved = json.loads(cat.read_text())
    assert saved["providers"]["claude-api"]["preferred"]["coding"] == "claude-opus-4-8"
    # …and survives a reload.
    model_catalog.reload()
    assert model_catalog.preferred("claude-api", "coding") == "claude-opus-4-8"


def test_set_model_enable_disable(tmp_overlays):
    model_catalog.set_model("claude-api", "claude-opus-4-8", enabled=False)
    assert model_catalog.is_enabled("claude-api", "claude-opus-4-8") is False
    enabled_ids = {m.id for m in model_catalog.provider_models("claude-api", enabled_only=True)}
    assert "claude-opus-4-8" not in enabled_ids
    # Unknown/free-text ids are not blocked.
    assert model_catalog.is_enabled("claude-api", "claude-future-9") is True


def test_set_model_adds_new_with_capabilities(tmp_overlays):
    model_catalog.set_model(
        "openai-api", "gpt-6", enabled=True, capabilities=["coding", "vision"])
    entry = model_catalog.provider_catalog("openai-api").get("gpt-6")
    assert entry is not None and entry.capabilities == ["coding", "vision"]


def test_pricing_write_through_to_flat_overlay(tmp_overlays):
    cat, price = tmp_overlays
    model_pricing.set_overlay_price("acme-1", (1.0, 2.0))
    # Flows through lookup() → effective_pricing.
    assert model_pricing.lookup("acme-1") == (1.0, 2.0)
    # Written to the flat price overlay (the single pricing store).
    assert "acme-1" in json.loads(price.read_text())
    # And surfaced as a catalog model (derived view stays in sync).
    assert model_catalog.provider_catalog("open-default").get("acme-1") is not None


def test_effective_model_uses_catalog_default(tmp_overlays):
    from app.providers.registry import effective_model, get_instance, get_provider_def
    model_catalog.set_preferred("claude-api", "default", "claude-haiku-4-5")
    inst = get_instance("claude-api")
    assert effective_model(inst, get_provider_def("claude-api")) == "claude-haiku-4-5"


def test_executor_runs_catalog_default_not_just_template(tmp_overlays):
    """The executor must *run* the catalog default, not only the template default
    (regression: __init__ bypassed effective_model and pinned the template model)."""
    from app.providers.model_executor import ModelExecutor
    from app.providers.registry import get_instance, get_provider_def
    model_catalog.set_preferred("openai-api", "default", "gpt-4o-mini")
    ex = ModelExecutor(get_provider_def("openai-api"), get_instance("openai-api"))
    assert ex._model == "gpt-4o-mini"  # catalog default, not the template's gpt-4o
