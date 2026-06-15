"""
providers/model_pricing.py — Known-model price book + effective-rate resolution.

Per-run cost is metered as Run.cost_usd from token counts × per-Mtok rates
(model_executor._compute_cost). Those rates used to come only from the provider
*template* (registry.ProviderDef.cost_*_per_mtok), so two things made estimates
wrong whenever the running model wasn't the template default:

  1. A built-in API provider whose model was overridden (e.g. claude-api moved
     off its default to a cheaper model) kept the template's rates.
  2. Custom providers hard-coded 0.0, so every custom-provider run reported $0.

This module fixes both by resolving the *effective* rate for the model actually
in use, in priority order:

    operator-set override  >  known-model price book  >  template default

`lookup(model)` answers "does the backend know this model's price?" so the UI can
pre-populate the two rate fields (and mark them "from registry"), or fall back to
asking the operator to enter them when the model is unknown.

The price book is a curated snapshot (USD per million tokens, input/output). It
is intentionally small and matched leniently (normalised + longest-prefix) so a
dated or vendor-prefixed id (`claude-opus-4-8-20260101`, `anthropic/claude-...`)
still resolves. Prices drift; operators can always override per instance.

Overlay file (Docker-friendly): the baked-in `_DEFAULT_PRICES` below is the
fallback. At import we overlay an optional JSON file (env `MODEL_PRICING_PATH`,
default `/data/model-pricing.json`) on top of it, so a Docker install can map a
newer price book over the volume to add or correct models with **no code change**.
The file is a flat object: `{"<model-id>": [in_per_mtok, out_per_mtok], ...}`.
File entries win over the baked-in defaults; malformed entries are skipped.

Prompt-cache rates: replayed input billed against a provider cache reads cheap
(≈0.1× base input) and the first write of a cache breakpoint a premium (≈1.25×).
Most entries don't carry explicit cache rates, so `cache_rates()` derives them
from the input rate by the Anthropic-typical multipliers below; an overlay entry
may instead give a **4-tuple** `[in, out, cache_read, cache_write]` to pin exact
rates (e.g. for OpenAI/Gemini, whose cached-input ratios differ). `lookup()` keeps
returning the `(in, out)` pair so existing callers are unaffected.

Call `reload()` after writing the file at runtime. OSS boundary: no import of
batonkeep_cloud.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_PRICING_PATH = os.environ.get("MODEL_PRICING_PATH", "/data/model-pricing.json")

# Default cache-rate multipliers off the base input rate, used when an entry has
# no explicit 4-tuple. Anthropic-typical: cache-read ≈0.1× input, cache-write
# (first store of a breakpoint) ≈1.25× input. Operators can pin exact per-model
# rates (incl. OpenAI/Gemini's different ratios) via a 4-tuple overlay entry.
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25

# (input_per_mtok, output_per_mtok) in USD per 1M tokens.
# Keep ids in the same normalised form lookup() produces (lowercased, vendor
# prefix stripped). Sourced 2026-06-10; verify against each vendor's pricing page.
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # ── Anthropic (Claude) ──────────────────────────────────────────────────
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    # ── OpenAI ──────────────────────────────────────────────────────────────
    "gpt-5.5": (5, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.2, 1.25),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o3": (2.0, 8.0),
    "o4-mini": (1.1, 4.4),
    # ── Google (Gemini) ─────────────────────────────────────────────────────
    "gemini-3.5-flash": (1.5, 9.0),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-3.1-flash-lite": (0.25,1.5),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.3, 2.5),
    "gemini-2.0-flash": (0.1, 0.4),
    # ── xAI (Grok) ──────────────────────────────────────────────────────────
    "grok-4.3": (1.25, 2.5),
    "grok-build-0.1": (1.0, 2.0),
    # ── Open-weight (common hosted rates; varies by host) ─────────────────────
    "meta-llama/llama-3.3-70b-instruct": (0.18, 0.18),
    "llama-3.3-70b": (0.18, 0.18),
}


def _load_overlay() -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Read the optional Docker-mapped overlay file. Missing/corrupt → ({}, {}).

    Returns `(base_prices, cache_prices)`: a 2-tuple entry sets only `(in, out)`;
    a 4-tuple entry additionally pins `(cache_read, cache_write)`.
    """
    try:
        with open(_PRICING_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("[model_pricing] failed to load %s: %s", _PRICING_PATH, exc)
        return {}, {}
    if not isinstance(data, dict):
        logger.error("[model_pricing] %s must be a JSON object", _PRICING_PATH)
        return {}, {}
    out: dict[str, tuple[float, float]] = {}
    cache: dict[str, tuple[float, float]] = {}
    for k, v in data.items():
        if isinstance(v, list | tuple) and len(v) in (2, 4):
            try:
                key = _normalise(str(k))
                out[key] = (float(v[0]), float(v[1]))
                if len(v) == 4:
                    cache[key] = (float(v[2]), float(v[3]))
            except (TypeError, ValueError):
                logger.warning("[model_pricing] skipping malformed entry %r", k)
        else:
            logger.warning("[model_pricing] skipping malformed entry %r", k)
    if out:
        logger.info("[model_pricing] overlaid %d model(s) from %s", len(out), _PRICING_PATH)
    return out, cache


# Effective price book: baked-in defaults with the overlay file layered on top.
_PRICES: dict[str, tuple[float, float]] = {}
# Explicit cache rates `(cache_read, cache_write)` for models that pin them via a
# 4-tuple overlay; models absent here get derived rates (see cache_rates()).
_CACHE_PRICES: dict[str, tuple[float, float]] = {}


def reload() -> None:
    """Rebuild the effective price book (defaults + overlay file). Call after a
    runtime write to the overlay; also run once at import."""
    _PRICES.clear()
    _CACHE_PRICES.clear()
    _PRICES.update(_DEFAULT_PRICES)
    base, cache = _load_overlay()
    _PRICES.update(base)
    _CACHE_PRICES.update(cache)


def _normalise(model: str) -> str:
    """Lowercase and strip a leading vendor prefix (e.g. 'anthropic/', 'openai/')."""
    m = model.strip().lower()
    # Keep meta-llama/... (the slug is part of the OpenAI-compatible model id);
    # only strip a known routing-vendor prefix.
    for prefix in ("anthropic/", "openai/", "google/", "x-ai/", "xai/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
            break
    return m


def lookup(model: str | None) -> tuple[float, float] | None:
    """
    Best-effort price lookup for a model id. Returns (in_per_mtok, out_per_mtok)
    when the backend recognises the model, else None.

    Matching is lenient: exact normalised match first, then longest-prefix match
    so dated/suffixed ids ('claude-opus-4-8-20260101') resolve to their base.
    """
    if not model:
        return None
    m = _normalise(model)
    exact = _PRICES.get(m)
    if exact is not None:
        return exact
    # Longest registered key that is a prefix of the requested id wins, so
    # 'claude-opus-4-8-20260101' picks 'claude-opus-4-8' over 'claude-opus-4'.
    best: tuple[str, tuple[float, float]] | None = None
    for key, rates in _PRICES.items():
        if m.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, rates)
    return best[1] if best else None


def cache_rates(model: str | None, in_rate: float) -> tuple[float, float]:
    """The `(cache_read, cache_write)` $/Mtok rates for a model.

    Explicit per-model rates (pinned via a 4-tuple overlay) win, matched leniently
    like `lookup()`; otherwise derive from `in_rate` by the default multipliers.
    `in_rate` is the already-resolved effective input rate (override/book/template),
    so a custom or operator-overridden model still gets coherent cache rates.
    """
    m = _normalise(model) if model else ""
    explicit = _CACHE_PRICES.get(m)
    if explicit is None and m:
        best: tuple[str, tuple[float, float]] | None = None
        for key, rates in _CACHE_PRICES.items():
            if m.startswith(key) and (best is None or len(key) > len(best[0])):
                best = (key, rates)
        explicit = best[1] if best else None
    if explicit is not None:
        return explicit
    return (in_rate * _CACHE_READ_MULT, in_rate * _CACHE_WRITE_MULT)


# Build the effective price book at import (defaults + optional overlay file).
reload()
