"""Tests for the known-model price book + effective-rate resolution."""
from __future__ import annotations

import importlib
import json

import pytest

from app.providers import model_pricing
from app.providers.registry import (
    effective_pricing,
    get_pricing_override,
    get_provider_def,
    set_pricing_override,
)


def test_lookup_exact_and_normalised():
    assert model_pricing.lookup("claude-opus-4-8") == (5.0, 25.0)
    assert model_pricing.lookup("CLAUDE-OPUS-4-8") == (5.0, 25.0)
    assert model_pricing.lookup("anthropic/claude-sonnet-4-6") == (3.0, 15.0)


def test_lookup_longest_prefix_for_dated_id():
    # Dated suffix resolves to the base id, and to the *most specific* base.
    assert model_pricing.lookup("claude-opus-4-8-20260101") == (5.0, 25.0)
    assert model_pricing.lookup("claude-opus-4-1-20250805") == (15.0, 75.0)


def test_lookup_unknown_returns_none():
    assert model_pricing.lookup("totally-made-up-model") is None
    assert model_pricing.lookup(None) is None
    assert model_pricing.lookup("") is None


def test_effective_pricing_priority_order():
    pdef = get_provider_def("claude-api")
    assert pdef is not None
    # Template default is the stale 15/75; the price book corrects it by model id.
    assert effective_pricing(pdef, "claude-api", "claude-opus-4-5") == (5.0, 25.0)
    # Tracks an overridden model.
    assert effective_pricing(pdef, "claude-api", "claude-haiku-4-5") == (1.0, 5.0)
    # Unknown model falls back to the template default.
    assert effective_pricing(pdef, "claude-api", "unknown-x") == (15.0, 75.0)


def test_pricing_override_wins(tmp_path, monkeypatch):
    inst = "claude-api"
    try:
        set_pricing_override(inst, (2.0, 8.0))
        assert get_pricing_override(inst) == (2.0, 8.0)
        pdef = get_provider_def("claude-api")
        # Override beats both the price book and the template.
        assert effective_pricing(pdef, inst, "claude-opus-4-8") == (2.0, 8.0)
    finally:
        set_pricing_override(inst, None)
        assert get_pricing_override(inst) is None


def test_overlay_file_extends_and_overrides(tmp_path, monkeypatch):
    overlay = tmp_path / "model-pricing.json"
    overlay.write_text(json.dumps({
        "brand-new-model": [7.0, 14.0],     # add
        "claude-opus-4-8": [9.0, 9.0],      # override a baked-in default
        "garbage": "not-a-pair",            # skipped
    }))
    monkeypatch.setattr(model_pricing, "_PRICING_PATH", str(overlay))
    try:
        model_pricing.reload()
        assert model_pricing.lookup("brand-new-model") == (7.0, 14.0)
        assert model_pricing.lookup("claude-opus-4-8") == (9.0, 9.0)
    finally:
        # Restore the baked-in book for other tests.
        monkeypatch.undo()
        model_pricing.reload()
        assert model_pricing.lookup("claude-opus-4-8") == (5.0, 25.0)
