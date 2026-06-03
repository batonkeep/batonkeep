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
