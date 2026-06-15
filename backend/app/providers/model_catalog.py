"""
providers/model_catalog.py — structured, provider-keyed API model catalog (P-0049).

The model SSOT for the API path. Where `model_pricing` is the flat $/Mtok price
book, this layers the *structure* on top: which models a provider offers, whether
each is enabled, its capability tags, and the provider's per-capability **preferred**
model (the field automated cost-aware routing — P-0048 lever 4, deferred per D-0045 —
will later consume). Manual selection uses it now via the model picker.

Two-file overlay seam (D-0045b — overlay JSON is the SSOT, the Settings UI is a thin
editor over it):
  - structure → `/data/model-catalog.json` (env `MODEL_CATALOG_PATH`), this module.
  - pricing   → `/data/model-pricing.json` (model_pricing's flat overlay). The catalog
    UI's pricing edits write *through* to that file (model_pricing.set_overlay_price)
    so they flow through `lookup()` → `effective_pricing` with no second pricing store.

Baked-in defaults are **derived**, not hand-maintained: every model in
`model_pricing._DEFAULT_PRICES` is bucketed to a provider by id prefix, seeded with
that provider's registry capability tags, and the provider's registry default model
becomes its `preferred.default`. The structured overlay then adds/overrides. A legacy
flat `model-pricing.json` is still honoured for pricing (back-compat).

OSS boundary: no import of batonkeep_cloud.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CATALOG_PATH = os.environ.get("MODEL_CATALOG_PATH", "/data/model-catalog.json")

# Model-facing capability vocabulary (D-0045c / P-0049 Q1): the P-0044/D-0033 routing
# tags plus model-facing additions (image/vision/realtime). Operators may use others;
# this set drives the per-capability **preferred** slots the UI exposes.
PREFERRED_CAPABILITIES: tuple[str, ...] = (
    "default", "coding", "synthesis", "longcontext", "image", "vision", "realtime",
)

# Provider template → model-id prefixes used to bucket the flat price book into the
# baked-in catalog. A model matching no prefix falls to the open-weight provider.
_PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude-api": ("claude",),
    "openai-api": ("gpt", "o1", "o3", "o4", "chatgpt"),
    "grok-api": ("grok",),
    "gemini-api": ("gemini",),
}
_FALLBACK_PROVIDER = "open-default"


@dataclass
class ModelEntry:
    id: str
    enabled: bool = True
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ProviderCatalog:
    template: str
    models: list[ModelEntry] = field(default_factory=list)
    # capability → preferred model id (subset of PREFERRED_CAPABILITIES keys).
    preferred: dict[str, str] = field(default_factory=dict)

    def get(self, model_id: str) -> ModelEntry | None:
        from app.providers.model_pricing import _normalise
        m = _normalise(model_id)
        return next((e for e in self.models if _normalise(e.id) == m), None)


# Effective catalog: provider template → ProviderCatalog. Built at import.
_CATALOG: dict[str, ProviderCatalog] = {}


def _provider_for(model_id: str) -> str:
    """Bucket a (normalised) model id to a provider template by id prefix."""
    for template, prefixes in _PROVIDER_PREFIXES.items():
        if any(model_id.startswith(p) for p in prefixes):
            return template
    return _FALLBACK_PROVIDER


def _baked_catalog() -> dict[str, ProviderCatalog]:
    """Derive the default catalog from the price book + registry (no hand-maintenance)."""
    from app.providers import model_pricing
    from app.providers.registry import get_provider_def

    out: dict[str, ProviderCatalog] = {}
    for model_id in model_pricing._DEFAULT_PRICES:
        template = _provider_for(model_id)
        pc = out.setdefault(template, ProviderCatalog(template=template))
        pdef = get_provider_def(template)
        caps = list(pdef.capability_tags) if pdef else []
        pc.models.append(ModelEntry(id=model_id, enabled=True, capabilities=caps))
    # Seed each provider's preferred.default from its registry default model.
    for template, pc in out.items():
        pdef = get_provider_def(template)
        if pdef and pdef.model:
            pc.preferred.setdefault("default", pdef.model)
    return out


def _load_overlay() -> dict:
    """Read the structured overlay. Missing/corrupt → {}.

    Schema: {"providers": {"<template>": {
        "models": [{"id", "enabled"?, "capabilities"?}, ...],
        "preferred": {"<capability>": "<model-id>", ...}}}}
    """
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("[model_catalog] failed to load %s: %s", _CATALOG_PATH, exc)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        logger.error("[model_catalog] %s must be {'providers': {...}}", _CATALOG_PATH)
        return {}
    return data["providers"]


def _apply_overlay(catalog: dict[str, ProviderCatalog], providers: dict) -> None:
    for template, body in providers.items():
        if not isinstance(body, dict):
            continue
        pc = catalog.setdefault(template, ProviderCatalog(template=template))
        for m in body.get("models", []) or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            existing = pc.get(m["id"])
            caps = m.get("capabilities")
            if existing is None:
                pc.models.append(ModelEntry(
                    id=str(m["id"]),
                    enabled=bool(m.get("enabled", True)),
                    capabilities=[str(c) for c in caps] if isinstance(caps, list) else [],
                ))
            else:
                if "enabled" in m:
                    existing.enabled = bool(m["enabled"])
                if isinstance(caps, list):
                    existing.capabilities = [str(c) for c in caps]
        pref = body.get("preferred")
        if isinstance(pref, dict):
            pc.preferred.update({str(k): str(v) for k, v in pref.items() if v})


def reload() -> None:
    """Rebuild the effective catalog (baked defaults + structured overlay). Also pulls
    any legacy flat-overlay pricing into the catalog as models, for back-compat."""
    _CATALOG.clear()
    _CATALOG.update(_baked_catalog())
    # Legacy flat pricing overlay (model-pricing.json): surface any ids it adds as
    # catalog models so an operator's existing bulk price file still populates the
    # picker (pricing itself already resolves via model_pricing.lookup).
    from app.providers import model_pricing
    flat, _cache = model_pricing._load_overlay()
    for model_id in flat:
        template = _provider_for(model_id)
        pc = _CATALOG.setdefault(template, ProviderCatalog(template=template))
        if pc.get(model_id) is None:
            pc.models.append(ModelEntry(id=model_id, enabled=True, capabilities=[]))
    _apply_overlay(_CATALOG, _load_overlay())


# ── Read API ────────────────────────────────────────────────────────────────────

def provider_catalog(template: str) -> ProviderCatalog:
    """The catalog for a provider template (empty ProviderCatalog if unknown)."""
    return _CATALOG.get(template) or ProviderCatalog(template=template)


def provider_models(template: str, *, enabled_only: bool = False) -> list[ModelEntry]:
    pc = provider_catalog(template)
    return [m for m in pc.models if m.enabled] if enabled_only else list(pc.models)


def preferred(template: str, capability: str = "default") -> str | None:
    """The provider's preferred model id for a capability, falling back to `default`."""
    pc = provider_catalog(template)
    return pc.preferred.get(capability) or pc.preferred.get("default")


def is_enabled(template: str, model_id: str) -> bool:
    """Whether a model is enabled for a provider. Unknown models default to enabled
    (free-text ids the catalog doesn't list are not blocked)."""
    entry = provider_catalog(template).get(model_id)
    return True if entry is None else entry.enabled


# ── Write API (the UI editor writes through these → overlay file + reload) ────────

def _persist(catalog: dict[str, ProviderCatalog]) -> None:
    out = {"providers": {
        t: {
            "models": [
                {"id": e.id, "enabled": e.enabled, "capabilities": e.capabilities}
                for e in pc.models
            ],
            "preferred": pc.preferred,
        }
        for t, pc in catalog.items()
    }}
    try:
        with open(_CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except OSError as exc:
        logger.error("[model_catalog] failed to persist %s: %s", _CATALOG_PATH, exc)


def set_model(
    template: str, model_id: str, *,
    enabled: bool | None = None, capabilities: list[str] | None = None,
) -> None:
    """Add or update a model's structure (enabled / capabilities) and persist."""
    pc = _CATALOG.setdefault(template, ProviderCatalog(template=template))
    entry = pc.get(model_id)
    if entry is None:
        entry = ModelEntry(id=model_id)
        pc.models.append(entry)
    if enabled is not None:
        entry.enabled = enabled
    if capabilities is not None:
        entry.capabilities = [str(c) for c in capabilities]
    _persist(_CATALOG)


def set_preferred(template: str, capability: str, model_id: str | None) -> None:
    """Set (or clear, when model_id is falsy) a provider's preferred model for a
    capability and persist."""
    pc = _CATALOG.setdefault(template, ProviderCatalog(template=template))
    if model_id:
        pc.preferred[capability] = model_id
    else:
        pc.preferred.pop(capability, None)
    _persist(_CATALOG)


# Build the effective catalog at import.
reload()
