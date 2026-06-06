"""
config.py — env-driven Settings.

DEPLOYMENT_MODE controls which credential modes are available:
  personal / oss  → plan-CLI + API + open-weight
  managed         → API + open-weight ONLY; plan-CLI hard-refused at config load (§1a)
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(str, Enum):
    personal = "personal"
    oss = "oss"
    managed = "managed"


class CredMode(str, Enum):
    plan = "plan"
    byo_key = "byo_key"
    hosted = "hosted"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Deployment ────────────────────────────────────────────────────────────
    deployment_mode: DeploymentMode = DeploymentMode.personal
    owner_id: str = "local"

    # ── Credential / routing defaults ─────────────────────────────────────────
    default_cred_mode: CredMode = CredMode.plan
    # Comma-separated ordered candidate list; "mock" ships as safe default
    default_candidates: str = "mock"

    # ── Concurrency ───────────────────────────────────────────────────────────
    per_provider_concurrency: int = 1
    max_concurrent_runs: int = 4
    run_timeout_seconds: int = 1800

    # ── Budget (P-0009 #2) ────────────────────────────────────────────────────
    # Daily spend cap in USD across all of an owner's runs. 0 = unlimited.
    # When today's metered spend reaches the cap, the router gracefully degrades
    # to zero-marginal-cost providers (subscription plan-CLIs + local models)
    # instead of hard-failing; it defers only if none are available.
    daily_budget_usd: float = 0.0

    # ── Terminal seam policy (D-0015 / P-0018) ───────────────────────────────
    # The PTY interactive-CLI seam drives full TUI sessions, a wider surface than
    # headless `cli -p`. These config knobs bound it (see app/cli_policy.py):
    #   terminal_seam_enabled    — master switch; off ⇒ the seam refuses to run.
    #   terminal_allowed_commands — comma-list of control commands the seam may
    #       SEND into the TUI (default-deny allowlist; e.g. "/usage,/status").
    #   terminal_allow_shell     — whether the driven CLI may auto-run shell/tool
    #       commands (maps to the CLI's skip-permission flag). Off ⇒ launched in a
    #       no-auto-approve mode so model-generated shell stays gated.
    #   terminal_policy_path     — optional JSON file to extend the above at runtime.
    terminal_seam_enabled: bool = False
    # Default allowlist covers all D-0016 single-shot meta commands so the
    # subscription-info / model-info read-only paths work out of the box.
    terminal_allowed_commands: str = "/usage,/status,/cost,/model"
    terminal_allow_shell: bool = False
    terminal_policy_path: str = ""

    # ── Cron / scheduled-task seam rule (D-0016 / P-0019) ────────────────────
    # Scheduled tasks ride the headless `cli -p` lane (the sanctioned, provider-
    # metered automation seam). Providers without a documented headless mode
    # (currently {grok}) are filtered from scheduled candidate rotation by
    # default — they're available for *manual* and interactive runs, just not
    # for autonomous cron. Flip this on to opt the user into the ToS risk and
    # keep no-headless providers in scheduled rotation (personal/self-host).
    cron_allow_no_headless_providers: bool = False

    # ── Behaviour ─────────────────────────────────────────────────────────────
    autonomous_tools: bool = True
    seed_examples: bool = True

    # ── Security ──────────────────────────────────────────────────────────────
    app_secret: str = ""

    # ── In-UI console (scoped actions: set models, run auth) ──────────────────
    # Off by default — it execs auth flows inside the container, so it's only
    # safe behind a token and never in managed mode (§ legal guardrail).
    enable_web_console: bool = False
    web_console_token: str = ""  # required to use the console when enabled

    @property
    def web_console_available(self) -> bool:
        return (
            self.enable_web_console
            and bool(self.web_console_token)
            and self.deployment_mode != DeploymentMode.managed
        )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:////data/batonkeep.db"

    # ── Outputs ───────────────────────────────────────────────────────────────
    outputs_dir: str = "/data/outputs"

    # ── Build sessions (M1) ─────────────────────────────────────────────────────
    # Base dir for sandboxed, git-init'd per-session workspaces (one subdir each).
    sessions_dir: str = "/data/sessions"
    # Base dir for published static bundles (M1.4). One subdir per published
    # artifact (named by its share token); served publicly at /api/share/{token}.
    publish_dir: str = "/data/publish"

    # ── Asset upload-in (M1.5, D-0010) ──────────────────────────────────────────
    # Files dropped into the chat land as real workspace files; limits are
    # env-configurable (D-0008 B). Extension allowlist (lowercased, no dots) and a
    # per-file max size. Images go to assets/, data files to data/.
    upload_max_bytes: int = 10_485_760  # 10 MiB
    upload_allowed_ext: str = "png,jpg,jpeg,svg,webp,csv,pdf,txt,md"

    # ── Optional API keys (metered / BYO-key providers) ──────────────────────
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    xai_api_key: Optional[str] = None

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def candidates_list(self) -> list[str]:
        return [c.strip() for c in self.default_candidates.split(",") if c.strip()]

    @property
    def upload_allowed_ext_set(self) -> set[str]:
        """Lowercased extension allowlist (no leading dots) for asset upload-in."""
        return {e.strip().lstrip(".").lower() for e in self.upload_allowed_ext.split(",") if e.strip()}

    @property
    def plan_cli_allowed(self) -> bool:
        """plan-CLI mode is structurally forbidden when DEPLOYMENT_MODE=managed (§1a)."""
        return self.deployment_mode != DeploymentMode.managed

    @model_validator(mode="after")
    def _assert_managed_mode_guardrail(self) -> "Settings":
        """
        Legal guardrail (§1a / §4.1): managed mode must NEVER instantiate plan-CLI.
        This is a structural check at config load, not a runtime promise.
        Raises ValueError if managed mode tries to enable plan-CLI candidates.
        """
        if self.deployment_mode == DeploymentMode.managed:
            # plan-CLI templates; candidates may be instance ids ("claude:work")
            # so compare on the template part before any ":".
            cli_candidates = {"claude", "grok", "agy", "codex"}
            templates = {c.split(":", 1)[0] for c in self.candidates_list}
            bad = cli_candidates.intersection(templates)
            if bad:
                raise ValueError(
                    f"DEPLOYMENT_MODE=managed forbids plan-CLI candidates: {bad}. "
                    "Remove them from DEFAULT_CANDIDATES or switch to personal/oss mode."
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
