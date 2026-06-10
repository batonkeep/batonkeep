"""
providers/registry.py — Provider catalog (§6.4).

Manages:
- The static provider definitions (cost rates, tier, kind, capability tags)
- Runtime health state (delegated to quota.py in P5; stubs here)
- The get(name) → Executor factory

All plan-CLI entries are refused when DEPLOYMENT_MODE=managed (§1a).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from app.config import get_settings
from app.providers.base import Executor

logger = logging.getLogger(__name__)
_settings = get_settings()


@dataclass
class ProviderDef:
    name: str
    kind: str          # openai_compatible | anthropic | cli | mock
    tier: str          # open | frontier | agent | mock
    # Capability tags for routing (§4.3)
    capability_tags: list[str] = field(default_factory=list)
    base_url: str | None = None
    model: str | None = None
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0
    env_key: str | None = None       # env var holding the API key
    cli_binary: str | None = None    # for kind=cli
    mode: str = "mock"                  # plan | api | open | mock
    # Sovereignty boundary (P-0009 #1): True iff inference runs on infrastructure
    # we control and the prompt never leaves the box (a local model). Confidential
    # work routes *only* to local providers and never falls back to a remote one.
    local: bool = False
    # Authentication requirement for openai_compatible providers.
    # "required" (default) — a key must be present in the credential store or env.
    # "none"               — no auth (Ollama, LM Studio on localhost).
    # "bearer"             — Authorization: Bearer header.
    # "api_key_header"     — x-api-key header.
    # All non-"none" values go through the same resolve_api_key path; the distinction
    # is only needed to skip the check for unauthenticated local endpoints.
    auth_type: str = "required"


# ── Static registry ────────────────────────────────────────────────────────────

_ALL_PROVIDERS: list[ProviderDef] = [
    # ── Mock (always available, credential-free) ──────────────────────────────
    ProviderDef(
        name="mock",
        kind="mock",
        tier="mock",
        capability_tags=["mock", "any"],
        mode="mock",
    ),
    # ── Plan-CLI providers (disabled in managed mode) ─────────────────────────
    ProviderDef(
        name="claude",
        kind="cli",
        tier="agent",
        capability_tags=["longcontext", "synthesis", "coding", "frontier", "realtime", "markets"],
        cli_binary="claude",
        mode="plan",
    ),
    ProviderDef(
        name="grok",
        kind="cli",
        tier="agent",
        capability_tags=["longcontext", "synthesis", "coding", "frontier", "realtime", "markets"],
        cli_binary="grok",
        mode="plan",
    ),
    ProviderDef(
        name="agy",
        kind="cli",
        tier="agent",
        capability_tags=["longcontext", "synthesis", "coding", "frontier", "realtime", "markets"],
        cli_binary="agy",
        mode="plan",
    ),
    ProviderDef(
        name="codex",
        kind="cli",
        tier="agent",
        capability_tags=["longcontext", "synthesis", "coding", "frontier", "realtime", "markets"],
        cli_binary="codex",
        mode="plan",
    ),
    # ── API providers (BYO-key; co-equal with plan-CLI for personal/oss) ──────
    ProviderDef(
        name="claude-api",
        kind="anthropic",
        tier="frontier",
        capability_tags=["synthesis", "coding", "longcontext", "frontier"],
        model="claude-opus-4-5",
        cost_in_per_mtok=15.0,
        cost_out_per_mtok=75.0,
        env_key="ANTHROPIC_API_KEY",
        mode="api",
    ),
    ProviderDef(
        name="openai-api",
        kind="openai_compatible",
        tier="frontier",
        capability_tags=["coding", "synthesis", "frontier"],
        model="gpt-4o",
        cost_in_per_mtok=2.5,
        cost_out_per_mtok=10.0,
        env_key="OPENAI_API_KEY",
        mode="api",
    ),
    # xAI Grok via its OpenAI-compatible API (base_url set so it doesn't inherit OPENAI_BASE_URL).
    ProviderDef(
        name="grok-api",
        kind="openai_compatible",
        tier="frontier",
        capability_tags=["realtime", "markets", "coding", "frontier"],
        base_url="https://api.x.ai/v1",
        model="grok-4.3",  # adjust to your enabled xAI model
        cost_in_per_mtok=3.0,
        cost_out_per_mtok=15.0,
        env_key="XAI_API_KEY",
        mode="api",
    ),
    # Google Gemini via its OpenAI-compatible endpoint.
    ProviderDef(
        name="gemini-api",
        kind="openai_compatible",
        tier="frontier",
        capability_tags=["longcontext", "synthesis", "frontier"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.5-flash",  # adjust to your enabled Gemini model
        cost_in_per_mtok=1.25,
        cost_out_per_mtok=10.0,
        env_key="GEMINI_API_KEY",
        mode="api",
    ),
    # ── Open-weight (credential-free to end user; requires OPENAI_BASE_URL) ───
    ProviderDef(
        name="open-default",
        kind="openai_compatible",
        tier="open",
        capability_tags=["open", "any"],
        model="meta-llama/Llama-3.3-70B-Instruct",
        cost_in_per_mtok=0.18,
        cost_out_per_mtok=0.18,
        env_key="OPENAI_API_KEY",
        base_url=None,   # set via OPENAI_BASE_URL env
        mode="open",
    ),
    # ── Local model (ollama) — sovereignty tier (P-0009 #1) ───────────────────
    # Runs entirely on the operator's box via ollama's OpenAI-compatible API, so
    # confidential work can be processed without any prompt leaving the machine.
    # Validated end-to-end (the 'Daily AI Ecosystem Brief' ran through the API
    # executor + our tool loop against gemma4:12b). The model is overridable per
    # instance / at runtime; the env key is a placeholder (ollama ignores it, but
    # the OpenAI client requires a non-empty value).
    ProviderDef(
        name="ollama",
        kind="openai_compatible",
        tier="open",
        capability_tags=["open", "any", "local", "synthesis"],
        model="gemma4:12b",
        cost_in_per_mtok=0.0,
        cost_out_per_mtok=0.0,
        env_key="OLLAMA_API_KEY",
        base_url="http://localhost:11434/v1",
        mode="open",
        local=True,
    ),
]

# Index by name for fast lookup
_REGISTRY: dict[str, ProviderDef] = {p.name: p for p in _ALL_PROVIDERS}

# All known template names (for validating console/auth targets).
ALL_TEMPLATE_NAMES: frozenset[str] = frozenset(_REGISTRY.keys())


# ── Provider instances (accounts) — Phase B (§ DESIGN-provider-instances) ────────
#
# A *template* (ProviderDef above) is the kind: executor class, cost, tags, default
# model. An *instance* is a usable credential pool with its own auth + its own
# cooldown state, so two subscriptions of the same vendor (e.g. two Claude accounts)
# can fail over independently.
#
# Instance id convention:
#   - bare template name ("claude", "mock")      → the default instance (auto-created)
#   - "<template>:<slug>" ("claude:work")        → an extra account (declared in config)
#
# Default config-dir override env vars per plan-CLI (verified against the installed
# binaries 2026-06-02): claude→CLAUDE_CONFIG_DIR, codex→CODEX_HOME, grok→GROK_HOME,
# agy→GEMINI_DIR. A non-default CLI instance MUST declare its own cli_config_dir.

_CLI_CONFIG_ENV: dict[str, str] = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "grok": "GROK_HOME",
    "agy": "GEMINI_DIR",
}


@dataclass
class ProviderInstance:
    id: str                       # "claude" (default) or "claude:work"
    template: str                 # "claude" — FK to ProviderDef.name
    label: str                    # UI display, e.g. "Claude (work)"
    # CLI accounts: own config dir, exported to the subprocess via this env var.
    cli_config_dir: str | None = None
    cli_config_env: str | None = None
    # API accounts: which stored-credential provider key backs this instance
    # (defaults to the template name → reuses the existing single-key behaviour).
    credential_provider: str | None = None
    # Optional per-instance overrides (else inherit from the template):
    model_override: str | None = None
    enabled: bool = True

    @property
    def is_default(self) -> bool:
        return self.id == self.template


def _default_instance(pdef: ProviderDef) -> ProviderInstance:
    """The implicit account whose id == template name (back-compat with bare names)."""
    return ProviderInstance(
        id=pdef.name,
        template=pdef.name,
        label=pdef.name,
        cli_config_env=_CLI_CONFIG_ENV.get(pdef.name),
        credential_provider=pdef.name,
    )


def _load_configured_instances() -> dict[str, ProviderInstance]:
    """
    Load extra (non-default) instances from a JSON file named by the
    PROVIDER_INSTANCES_CONFIG env var. Missing/empty file → no extra instances.
    Schema: {"instances": [{"id","template","label",...}]}
    """
    path = os.environ.get("PROVIDER_INSTANCES_CONFIG")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("[registry] failed to load PROVIDER_INSTANCES_CONFIG=%s: %s", path, exc)
        return {}

    out: dict[str, ProviderInstance] = {}
    for entry in raw.get("instances", []):
        try:
            template = entry["template"]
            inst_id = entry.get("id") or f"{template}:{entry['slug']}"
        except KeyError as exc:
            logger.error("[registry] instance config entry missing %s: %r", exc, entry)
            continue
        if template not in _REGISTRY:
            logger.error("[registry] instance %s references unknown template %s", inst_id, template)
            continue
        cli_env = entry.get("cli_config_env") or _CLI_CONFIG_ENV.get(template)
        out[inst_id] = ProviderInstance(
            id=inst_id,
            template=template,
            label=entry.get("label", inst_id),
            cli_config_dir=entry.get("cli_config_dir"),
            cli_config_env=cli_env,
            credential_provider=entry.get("credential_provider", inst_id),
            model_override=entry.get("model_override"),
            enabled=entry.get("enabled", True),
        )
    logger.info("[registry] loaded %d configured provider instance(s)", len(out))
    return out


_CONFIGURED_INSTANCES: dict[str, ProviderInstance] = _load_configured_instances()


# ── Runtime model overrides (set from the UI console) ────────────────────────
# Persisted to a small JSON file so a chosen model survives restarts, separate
# from the declarative PROVIDER_INSTANCES_CONFIG. Resolution order for an
# instance's model: runtime override > instance.model_override > template default.

_MODEL_OVERRIDES_PATH = os.environ.get("MODEL_OVERRIDES_PATH", "/data/model-overrides.json")


def _load_model_overrides() -> dict[str, str]:
    try:
        with open(_MODEL_OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, json.JSONDecodeError):
        return {}


_MODEL_OVERRIDES: dict[str, str] = _load_model_overrides()


def get_model_override(instance_id: str) -> str | None:
    return _MODEL_OVERRIDES.get(instance_id)


def set_model_override(instance_id: str, model: str | None) -> None:
    """Set (or clear, when model is falsy) an instance's runtime model + persist."""
    if model:
        _MODEL_OVERRIDES[instance_id] = model
    else:
        _MODEL_OVERRIDES.pop(instance_id, None)
    try:
        with open(_MODEL_OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(_MODEL_OVERRIDES, f, indent=2)
    except OSError as exc:
        logger.error("[registry] failed to persist model overrides: %s", exc)


# ── Runtime capability-tag overrides (P-0044) ────────────────────────────────
# Same pattern as model overrides: operators set a built-in provider's routing
# tags from the UI so they can align a provider to what their tasks require.
# Resolution for a provider's effective tags: override (if set) > template tags.
# Custom providers carry their tags in their own record (custom_providers.py).

_TAGS_OVERRIDES_PATH = os.environ.get("TAGS_OVERRIDES_PATH", "/data/tags-overrides.json")


def _load_tags_overrides() -> dict[str, list[str]]:
    try:
        with open(_TAGS_OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {
            str(k): [str(t) for t in v]
            for k, v in data.items()
            if isinstance(v, list) and v
        }
    except (OSError, json.JSONDecodeError):
        return {}


_TAGS_OVERRIDES: dict[str, list[str]] = _load_tags_overrides()


def get_tags_override(name: str) -> list[str] | None:
    return _TAGS_OVERRIDES.get(name)


def set_tags_override(name: str, tags: list[str] | None) -> None:
    """Set (or clear, when tags is empty) a provider's routing-tag override + persist."""
    cleaned = [t.strip() for t in (tags or []) if t and t.strip()]
    if cleaned:
        _TAGS_OVERRIDES[name] = cleaned
    else:
        _TAGS_OVERRIDES.pop(name, None)
    try:
        with open(_TAGS_OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(_TAGS_OVERRIDES, f, indent=2)
    except OSError as exc:
        logger.error("[registry] failed to persist tags overrides: %s", exc)


def effective_capability_tags(pdef: ProviderDef) -> list[str]:
    """The routing tags a provider actually matches on: override > template tags."""
    return get_tags_override(pdef.name) or pdef.capability_tags


# ── Headless capability (D-0016 / P-0019) ──────────────────────────────────────
# Plan-CLI templates that do NOT ship a documented headless `-p` mode. Scheduled/
# cron tasks ride the headless lane (sanctioned + provider-metered), so any such
# template is filtered out of scheduled candidate rotation by default — see
# settings.cron_allow_no_headless_providers for the user ToS-risk opt-in.
#
# As of 2026-06-06 this set is EMPTY: all four plan CLIs have first-party headless
# modes — claude `-p`, codex `exec`, agy `-p`, and **grok `-p/--single`** (verified
# live: `--output-format plain|json|streaming-json`, `--best-of-n`/`--check`
# "headless only"; xAI officially promotes it for scripts/automation, SuperGrok +
# X Premium Plus). The earlier "grok has no headless mode" read was an overreach
# from the D-0015 probe, which only showed that grok can't run *slash commands*
# (e.g. `/usage`) via `-p` — task *prompts* run headless fine. Slash-command meta
# still uses the full-TTY single-shot path; that's orthogonal to this set.
# The mechanism stays in place for any future provider that genuinely lacks `-p`.
_NO_HEADLESS_CLI_TEMPLATES: frozenset[str] = frozenset()


def is_headless_capable(candidate: str) -> bool:
    """True if the candidate (provider template or instance id like 'claude:work')
    supports headless `cli -p` execution. The check is on the template part, so
    multi-account instances inherit their template's capability."""
    template = str(candidate).split(":", 1)[0]
    return template not in _NO_HEADLESS_CLI_TEMPLATES


# ── Execution seams (D-0016 posture) ────────────────────────────────────────────
# A CLI provider has THREE seams, but they are not user-selectable task lanes:
#   1. headless `cli -p`  — the ONLY seam for task turns (get_executor below).
#   2. full-TTY single-shot — drive ONE read-only meta command (e.g. /usage) and
#      read one screen. Automated + internal only (get_interactive_executor),
#      never exposed as a task executor a user can flip on.
#   3. web-TTY — a live PTY the human drives (app/web_tty.py, /ws/tty).
# The earlier user-facing "exec-seam = terminal" override (D-0015 slice 3) let a
# user route task turns through seam #2's autonomous TUI driver; that produced
# scraped agent narration as the "result" and re-crossed the ToS line D-0016 drew.
# Removed: task execution is always headless; the interactive driver survives only
# for internal single-shot meta capture.


def effective_model(inst: ProviderInstance, pdef: ProviderDef) -> str | None:
    """
    The model an instance will actually use.

    - CLI plans own their model via their own config dir (set through the CLI's
      interactive picker). We read it for display; the runtime override does NOT
      apply to CLIs (it would be a competing source of truth).
    - API providers have no interactive UI, so the runtime override applies.
    """
    if pdef.kind == "cli":
        return cli_configured_model(inst, pdef)
    return get_model_override(inst.id) or inst.model_override or pdef.model


# Where each plan-CLI persists its selected model inside its config dir.
_CLI_DEFAULT_DIR = {"claude": ".claude", "grok": ".grok", "agy": ".gemini", "codex": ".codex"}
_CLI_MODEL_KEY = {"codex": "model", "grok": "default_model"}  # in <dir>/config.toml


def _cli_auth_home() -> str:
    """Home dir under which a default plan-CLI instance's auth lives.

    Plan-CLIs run as the low-priv `sandbox` user (HOME=/home/agent) through the
    setuid spawner, so their auth dirs (~/.claude, ~/.codex, …) sit under THAT
    user's home — not the backend process's HOME (/home/batond). Resolving against
    the process HOME made /api/providers report every plan-CLI disconnected even
    though task runs (which spawn as `sandbox`) authenticated fine. When the
    sandbox split is active, resolve against the sandbox user's home; otherwise
    (local dev / tests / non-container) fall back to the process HOME.
    """
    from app import sandbox

    if sandbox.available():
        try:
            import pwd

            return pwd.getpwnam("sandbox").pw_dir
        except (KeyError, ImportError):
            return "/home/agent"
    return os.environ.get("HOME") or os.path.expanduser("~")


def cli_configured_model(inst: ProviderInstance, pdef: ProviderDef) -> str | None:
    """Best-effort read of a plan-CLI's currently selected model from its config."""
    key = _CLI_MODEL_KEY.get(pdef.name)
    if not key:
        return None  # claude (subscription default/alias), agy (auto) — not persisted simply
    if inst.cli_config_dir:
        cfg_dir = inst.cli_config_dir
    else:
        home = _cli_auth_home()
        sub = _CLI_DEFAULT_DIR.get(pdef.name)
        cfg_dir = os.path.join(home, sub) if sub else None
    if not cfg_dir:
        return None
    path = os.path.join(cfg_dir, "config.toml")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith(key) and "=" in s:
                    k, v = s.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def get_instance(instance_id: str) -> ProviderInstance | None:
    """
    Resolve an instance id → ProviderInstance, honouring DEPLOYMENT_MODE.

    Returns None when the underlying template is unknown or unavailable (e.g. a
    plan-CLI instance under managed mode), or when a "template:slug" id was used
    without being declared in the instances config.
    """
    # Declared (non-default) instance.
    inst = _CONFIGURED_INSTANCES.get(instance_id)
    if inst is not None:
        if not inst.enabled:
            return None
        # Respect managed-mode plan-CLI refusal via the template lookup.
        if get_provider_def(inst.template) is None:
            return None
        return inst

    # Undeclared "template:slug" — refuse rather than silently colliding on auth.
    if ":" in instance_id:
        logger.warning(
            "[registry] instance %s not declared in PROVIDER_INSTANCES_CONFIG", instance_id
        )
        return None

    # Bare template name → the implicit default instance.
    pdef = get_provider_def(instance_id)
    if pdef is None:
        return None
    return _default_instance(pdef)


def list_instances() -> list[ProviderInstance]:
    """
    All usable instances: one default per available template, plus declared extras.
    Plan-CLI templates (and their instances) are excluded in managed mode.
    """
    result: list[ProviderInstance] = []
    for pdef in list_providers():
        result.append(_default_instance(pdef))
    for inst in _CONFIGURED_INSTANCES.values():
        if not inst.enabled:
            continue
        if get_provider_def(inst.template) is None:
            continue
        result.append(inst)
    return result


def local_candidate_ids() -> list[str]:
    """Instance ids of every available local (sovereignty-tier) provider — the
    only eligible candidates for confidential work (P-0009 #1). Default instances
    of local templates, plus any declared local instances."""
    ids: list[str] = []
    for inst in list_instances():
        pdef = get_provider_def(inst.template)
        if pdef is not None and pdef.local:
            ids.append(inst.id)
    return ids


def is_local_instance(instance_id: str) -> bool:
    """True iff the instance resolves to a local (sovereignty-tier) provider."""
    inst = get_instance(instance_id)
    if inst is None:
        return False
    pdef = get_provider_def(inst.template)
    return bool(pdef and pdef.local)


def list_providers(include_cli: bool | None = None) -> list[ProviderDef]:
    """
    Return all provider definitions, optionally filtered.
    When DEPLOYMENT_MODE=managed, plan-CLI providers are excluded.
    """
    allowed_cli = _settings.plan_cli_allowed
    result = []
    for p in _ALL_PROVIDERS:
        if p.kind == "cli" and not allowed_cli:
            logger.debug("Excluding plan-CLI provider %s (managed mode)", p.name)
            continue
        if include_cli is not None:
            if include_cli and p.kind != "cli":
                continue
            if not include_cli and p.kind == "cli":
                continue
        result.append(p)
    return result


def get_provider_def(name: str) -> ProviderDef | None:
    pdef = _REGISTRY.get(name)
    if pdef is None:
        return None
    if pdef.kind == "cli" and not _settings.plan_cli_allowed:
        logger.warning("Provider %s requested but plan-CLI is disabled (managed mode)", name)
        return None
    return pdef


async def is_provider_connected(pdef: ProviderDef) -> bool:
    """Back-compat shim: connectivity of a template's default instance."""
    return await is_instance_connected(_default_instance(pdef))


async def is_instance_connected(inst: ProviderInstance) -> bool:
    """
    Whether a specific instance (account) is usable right now (independent of cooldown):
      - mock                : always connected
      - cli (plan)          : official binary on PATH AND this account's auth dir present
      - openai_compatible / : a key is resolvable for this account's credential provider
        anthropic
    Used to decide `healthy` in /api/providers so unconfigured accounts don't
    falsely report green.
    """
    pdef = get_provider_def(inst.template)
    if pdef is None:
        return False

    if pdef.kind == "mock":
        return True

    if pdef.kind == "cli":
        import shutil

        from app import sandbox

        if not (pdef.cli_binary and shutil.which(pdef.cli_binary)):
            return False
        # Logged-in heuristic: the account's auth dir exists. The check runs through
        # the sandbox spawner (as the `sandbox` user) because the CLI auth lives under
        # /home/agent, which the control-plane `batond` user cannot traverse — a direct
        # os.path.exists would always report "offline" even when the CLI is logged in.
        if inst.cli_config_dir:
            return await sandbox.path_exists(inst.cli_config_dir)
        # Default instance → the CLI's own auth dir under the sandbox user's HOME
        # (where headless task turns authenticate), not the backend process HOME.
        home = _cli_auth_home()
        sub = _CLI_DEFAULT_DIR.get(pdef.name)
        return await sandbox.path_exists(os.path.join(home, sub)) if sub else True

    if pdef.kind in ("openai_compatible", "anthropic"):
        from app.credentials import resolve_api_key

        cred_provider = inst.credential_provider or pdef.name
        return bool(await resolve_api_key(cred_provider, pdef.env_key))

    return False


def get_executor(instance_id: str) -> Executor | None:
    """
    Instantiate the appropriate Executor for the given instance id (e.g. "claude"
    or "claude:work"). Returns None if the instance/template is unknown or
    unavailable in the current mode. The executor's `name` is the instance id, so
    run records and cooldown state are keyed per-account.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return None
    pdef = get_provider_def(inst.template)
    if pdef is None:
        return None

    if pdef.kind == "mock":
        from app.providers.mock import MockExecutor
        return MockExecutor(name=inst.id)

    if pdef.kind == "cli":
        # Task turns ALWAYS run headless `cli -p` (D-0016 posture). The interactive
        # full-TTY driver is never a task executor — it's automated-internal only
        # (get_interactive_executor, for single-shot meta like /usage). Live human
        # terminals go through the web-TTY lane (app/web_tty.py), not here.
        try:
            from app.providers.cli_executor import CLIExecutor
            return CLIExecutor(pdef, instance=inst)
        except ImportError:
            logger.warning("CLIExecutor not yet available (P4)")
            return None

    if pdef.kind in ("openai_compatible", "anthropic"):
        # Wired in P3
        try:
            from app.providers.model_executor import ModelExecutor
            return ModelExecutor(pdef, instance=inst)
        except ImportError:
            logger.warning("ModelExecutor not yet available (P3)")
            return None

    return None


def get_interactive_executor(instance_id: str) -> Executor | None:
    """The full-TTY single-shot driver for a CLI instance — automated-internal only.

    Used to drive ONE read-only meta command (e.g. /usage) and read one screen
    (app/subscription_usage.py). NOT a task executor: get_executor() never returns
    this, so user task turns can't be routed through autonomous TUI driving. Returns
    None for non-CLI / unknown instances.
    """
    inst = get_instance(instance_id)
    if inst is None:
        return None
    pdef = get_provider_def(inst.template)
    if pdef is None or pdef.kind != "cli":
        return None
    try:
        from app.providers.cli_interactive import CLIInteractiveExecutor
        return CLIInteractiveExecutor(pdef, instance=inst)
    except ImportError:
        logger.warning("CLIInteractiveExecutor unavailable")
        return None
