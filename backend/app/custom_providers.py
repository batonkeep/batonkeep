"""
custom_providers.py — Runtime-mutable custom provider overlay (D-0026).

Operators can add local/Ollama/open-API endpoints from the Settings UI without
a backend deploy. Custom providers are stored in a JSON file on the /data volume
(same pattern as model-overrides.json) and injected into the registry at startup
+ on every CRUD write via reload_custom_providers().

Design decisions (D-0026):
  - Built-in catalogue stays hardcoded; this is an *overlay* only (Option C + A).
  - Custom providers behave as openai_compatible kind; the existing ModelExecutor
    dispatches them without changes.
  - Credentials (API keys) are stored separately in the Fernet credential store
    (credentials.py) keyed by the custom provider's id — same as built-in API
    providers. This module stores only auth_type + env_key hint, never the key.
  - Sovereignty: custom providers with local=True are included in local_candidate_ids()
    for confidential work routing (P-0009 #1).
  - OSS boundary: no import or reference to batonkeep_cloud.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# Persisted config: /data/custom-providers.json (operator can override via env).
_CUSTOM_PROVIDERS_PATH = os.environ.get(
    "CUSTOM_PROVIDERS_PATH", "/data/custom-providers.json"
)

# Built-in provider names that custom providers may not shadow (slug conflict guard).
_BUILTIN_NAMES: frozenset[str] = frozenset()  # populated lazily on first call

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")  # lowercase alphanum + hyphens


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CustomProvider:
    """
    A user-defined provider endpoint — stored in custom-providers.json.

    Converts to a ProviderDef for routing/executor dispatch via to_provider_def().
    The API key (if any) is stored separately in credentials.py; only the auth_type
    and env_key hint live here.
    """
    id: str                  # slug — must be unique across built-ins + customs
    label: str               # human display name (e.g. "My Ollama")
    base_url: str            # endpoint (e.g. "http://localhost:11434/v1")
    default_model: str       # first/default model (e.g. "gemma4:12b")
    # auth_type: none | bearer | api_key_header
    # "none"            — Ollama (no auth needed)
    # "bearer"          — Authorization: Bearer <key>
    # "api_key_header"  — x-api-key: <key> (some self-hosted APIs)
    auth_type: str = "none"
    # Optional env var hint to resolve the key from the environment instead of
    # (or as a fallback to) the Fernet credential store.
    env_key: str | None = None
    # True → inference stays on the operator's box; eligible for confidential routing.
    local: bool = False
    enabled: bool = True
    # Optional extra model names (comma-separated) for display / routing tags.
    extra_models: str = ""
    # Routing capability tags (P-0044). When the operator sets these in the UI they
    # decide which tasks route here; empty falls back to the auto default below.
    capability_tags: list[str] = field(default_factory=list)
    # Per-Mtok cost (USD), input/output. Pre-populated from the known-model price
    # book when default_model is recognised, else operator-entered. 0.0 means
    # unknown/free (the prior behaviour) and effective_pricing still falls back to
    # the price book by model id at metering time.
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0

    def to_provider_def(self):
        """Return a ProviderDef that the registry can add to its catalogue."""
        from app.providers.registry import ProviderDef

        # Operator-set tags win; otherwise fall back to the sensible auto default.
        if self.capability_tags:
            capability_tags = list(self.capability_tags)
        else:
            capability_tags = ["any"]
            if self.local:
                capability_tags += ["local"]
            # Custom providers inherit the "open" tag used for open-weight models.
            capability_tags.append("open")

        return ProviderDef(
            name=self.id,
            kind="openai_compatible",
            tier="open" if self.local else "frontier",
            capability_tags=capability_tags,
            base_url=self.base_url or None,
            model=self.default_model or None,
            cost_in_per_mtok=self.cost_in_per_mtok,
            cost_out_per_mtok=self.cost_out_per_mtok,
            env_key=self.env_key or None,
            mode="open",
            local=self.local,
            # Pass auth_type so the executor can skip the credential check for
            # unauthenticated local endpoints (Ollama, LM Studio on localhost).
            auth_type=self.auth_type,
        )


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_raw() -> list[dict]:
    """Read the JSON file from disk. Returns [] if missing or corrupt."""
    try:
        with open(_CUSTOM_PROVIDERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.error("[custom_providers] expected a JSON list in %s", _CUSTOM_PROVIDERS_PATH)
            return []
        return data
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("[custom_providers] failed to load %s: %s", _CUSTOM_PROVIDERS_PATH, exc)
        return []


def _save_raw(providers: list[CustomProvider]) -> None:
    """Persist the current list to disk (atomic-ish: write then rename)."""
    data = [asdict(p) for p in providers]
    tmp = _CUSTOM_PROVIDERS_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_CUSTOM_PROVIDERS_PATH) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _CUSTOM_PROVIDERS_PATH)
    except OSError as exc:
        logger.error("[custom_providers] failed to persist %s: %s", _CUSTOM_PROVIDERS_PATH, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_custom_providers() -> list[CustomProvider]:
    """Read all custom providers from disk."""
    result: list[CustomProvider] = []
    for entry in _load_raw():
        try:
            cp = CustomProvider(**{
                k: v for k, v in entry.items()
                if k in CustomProvider.__dataclass_fields__
            })
            result.append(cp)
        except (TypeError, KeyError) as exc:
            logger.error("[custom_providers] skipping malformed entry %r: %s", entry, exc)
    return result


# ── In-memory store + CRUD ────────────────────────────────────────────────────

_PROVIDERS: list[CustomProvider] = []

# Names we most recently injected into the registry.
# Used by _inject_into_registry to clean up exactly what it added —
# without this, recomputing from _ALL_PROVIDERS after a previous injection
# would include injected names in "static_names" and fail to remove them.
_INJECTED_NAMES: set[str] = set()


def _builtin_names() -> frozenset[str]:
    global _BUILTIN_NAMES
    if not _BUILTIN_NAMES:
        from app.providers.registry import ALL_TEMPLATE_NAMES
        # Use the known-constant set; avoid a circular import by accessing it lazily.
        _BUILTIN_NAMES = ALL_TEMPLATE_NAMES
    return _BUILTIN_NAMES


def init_custom_providers() -> None:
    """Load from disk into _PROVIDERS and inject into the registry. Call at startup."""
    global _PROVIDERS
    _PROVIDERS = [p for p in load_custom_providers() if p.enabled]
    _inject_into_registry(_PROVIDERS)
    logger.info("[custom_providers] loaded %d custom provider(s)", len(_PROVIDERS))


def _inject_into_registry(providers: list[CustomProvider]) -> None:
    """Merge the custom provider list into the live registry module globals.

    Uses _INJECTED_NAMES to track exactly what was added on the previous call,
    so teardown is precise regardless of how many times this is invoked.
    """
    global _INJECTED_NAMES
    from app.providers import registry as reg

    # Remove every name we added last time — clean slate before re-injecting.
    for name in list(_INJECTED_NAMES):
        reg._REGISTRY.pop(name, None)
        reg._ALL_PROVIDERS[:] = [p for p in reg._ALL_PROVIDERS if p.name != name]
    _INJECTED_NAMES = set()

    # Inject the new set.
    for cp in providers:
        pdef = cp.to_provider_def()
        reg._ALL_PROVIDERS.append(pdef)
        reg._REGISTRY[pdef.name] = pdef
        _INJECTED_NAMES.add(pdef.name)
    reg.ALL_TEMPLATE_NAMES = frozenset(reg._REGISTRY.keys())


def reload_custom_providers() -> None:
    """Re-read from disk and re-inject into the live registry (called after CRUD)."""
    global _PROVIDERS
    _PROVIDERS = [p for p in load_custom_providers() if p.enabled]
    _inject_into_registry(_PROVIDERS)
    logger.info("[custom_providers] reloaded — %d active provider(s)", len(_PROVIDERS))


def list_all_custom_providers() -> list[CustomProvider]:
    """Return all custom providers (including disabled ones), fresh from disk."""
    return load_custom_providers()


# ── Validation ────────────────────────────────────────────────────────────────

def _clean_tags(tags: list[str] | None) -> list[str]:
    """Normalise routing tags: trim, drop blanks, de-dupe, preserve order."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        s = t.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _validate_id(cp_id: str) -> str | None:
    """Return an error message if the id is invalid, else None."""
    if not _ID_RE.match(cp_id):
        return (
            "id must be lowercase alphanumeric with hyphens (1–63 chars),"
            " starting with a letter or digit"
        )
    if cp_id in _builtin_names():
        return f"id '{cp_id}' conflicts with a built-in provider name"
    return None


# ── CRUD ──────────────────────────────────────────────────────────────────────

class CustomProviderError(ValueError):
    """Raised for invalid create/update operations."""


def create_custom_provider(
    cp_id: str,
    label: str,
    base_url: str,
    default_model: str,
    auth_type: str = "none",
    env_key: str | None = None,
    local: bool = False,
    extra_models: str = "",
    capability_tags: list[str] | None = None,
    cost_in_per_mtok: float = 0.0,
    cost_out_per_mtok: float = 0.0,
) -> CustomProvider:
    """Add a new custom provider. Raises CustomProviderError on validation failure."""
    cp_id = cp_id.strip()
    err = _validate_id(cp_id)
    if err:
        raise CustomProviderError(err)

    existing = load_custom_providers()
    if any(p.id == cp_id for p in existing):
        raise CustomProviderError(f"A custom provider with id '{cp_id}' already exists")

    if not base_url.strip():
        raise CustomProviderError("base_url is required")
    if not default_model.strip():
        raise CustomProviderError("default_model is required")
    if auth_type not in ("none", "bearer", "api_key_header"):
        raise CustomProviderError("auth_type must be none | bearer | api_key_header")
    if cost_in_per_mtok < 0 or cost_out_per_mtok < 0:
        raise CustomProviderError("cost per Mtok cannot be negative")

    cp = CustomProvider(
        id=cp_id,
        label=label.strip() or cp_id,
        base_url=base_url.strip(),
        default_model=default_model.strip(),
        auth_type=auth_type,
        env_key=(env_key or "").strip() or None,
        local=local,
        enabled=True,
        extra_models=extra_models.strip(),
        capability_tags=_clean_tags(capability_tags),
        cost_in_per_mtok=float(cost_in_per_mtok),
        cost_out_per_mtok=float(cost_out_per_mtok),
    )
    existing.append(cp)
    _save_raw(existing)
    reload_custom_providers()
    logger.info("[custom_providers] created provider id=%s", cp_id)
    return cp


def update_custom_provider(
    cp_id: str,
    *,
    label: str | None = None,
    base_url: str | None = None,
    default_model: str | None = None,
    auth_type: str | None = None,
    env_key: str | None = None,
    local: bool | None = None,
    enabled: bool | None = None,
    extra_models: str | None = None,
    capability_tags: list[str] | None = None,
    cost_in_per_mtok: float | None = None,
    cost_out_per_mtok: float | None = None,
) -> CustomProvider:
    """Update fields of an existing custom provider."""
    existing = load_custom_providers()
    cp = next((p for p in existing if p.id == cp_id), None)
    if cp is None:
        raise CustomProviderError(f"Custom provider '{cp_id}' not found")

    if label is not None:
        cp.label = label.strip() or cp.label
    if base_url is not None:
        if not base_url.strip():
            raise CustomProviderError("base_url cannot be empty")
        cp.base_url = base_url.strip()
    if default_model is not None:
        if not default_model.strip():
            raise CustomProviderError("default_model cannot be empty")
        cp.default_model = default_model.strip()
    if auth_type is not None:
        if auth_type not in ("none", "bearer", "api_key_header"):
            raise CustomProviderError("auth_type must be none | bearer | api_key_header")
        cp.auth_type = auth_type
    if env_key is not None:
        cp.env_key = env_key.strip() or None
    if local is not None:
        cp.local = local
    if enabled is not None:
        cp.enabled = enabled
    if extra_models is not None:
        cp.extra_models = extra_models.strip()
    if capability_tags is not None:
        cp.capability_tags = _clean_tags(capability_tags)
    if cost_in_per_mtok is not None:
        if cost_in_per_mtok < 0:
            raise CustomProviderError("cost per Mtok cannot be negative")
        cp.cost_in_per_mtok = float(cost_in_per_mtok)
    if cost_out_per_mtok is not None:
        if cost_out_per_mtok < 0:
            raise CustomProviderError("cost per Mtok cannot be negative")
        cp.cost_out_per_mtok = float(cost_out_per_mtok)

    _save_raw(existing)
    reload_custom_providers()
    logger.info("[custom_providers] updated provider id=%s", cp_id)
    return cp


def delete_custom_provider(cp_id: str) -> bool:
    """Remove a custom provider by id. Returns True if it existed."""
    existing = load_custom_providers()
    new_list = [p for p in existing if p.id != cp_id]
    if len(new_list) == len(existing):
        return False  # not found
    _save_raw(new_list)
    reload_custom_providers()
    logger.info("[custom_providers] deleted provider id=%s", cp_id)
    return True
