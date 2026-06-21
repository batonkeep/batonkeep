"""
config.py — env-driven Settings.

DEPLOYMENT_MODE controls which credential modes are available:
  personal / oss  → plan-CLI + API + open-weight
  managed         → API + open-weight ONLY; plan-CLI hard-refused at config load (§1a)
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import model_validator
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

    # ── Run retry (P-0025 #2) ─────────────────────────────────────────────────
    # Bounded in-process retry for *transient* failures: a run whose candidate
    # chain exhausts on non-quota errors (failover on) is retried up to
    # max_run_retries times with exponential backoff before being marked failed.
    # Rate-limit/cooling exhaustion is NOT a retry case — it defers and the
    # scheduler's deferred-sweep re-enqueues it. Durable cross-restart requeue is
    # a later managed-scale graduation (D-0021); this is the in-process slice.
    max_run_retries: int = 2
    retry_backoff_seconds: float = 2.0

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

    # ── Ledger summarization (D-0017 thread 1) ────────────────────────────────
    # Opt-in: when enabled, a cheap model maintains the SESSION.md ## Summary so
    # provider switches are richly primed (deterministic Activity log is always on).
    # Confidential sessions never use a remote model — they summarize on a local
    # model or skip. Cadence: on provider-switch + on-demand.
    ledger_summary_enabled: bool = False
    # Optional explicit summarizer instance; empty → fall back to the session's
    # current provider (or, for confidential sessions, an available local one).
    ledger_summary_provider: str = ""
    ledger_summary_max_chars: int = 1200

    # ── Security ──────────────────────────────────────────────────────────────
    app_secret: str = ""

    # ── App-level auth (D-0023, resolves P-0026) ──────────────────────────────
    # Single-operator login gate for personal/oss. When APP_PASSWORD is set the
    # whole API requires a signed session cookie (see auth.py) — this protects
    # the *data*, not just the console. Empty ⇒ no gate (backward-compatible dev
    # default). Multi-user accounts are a managed concern (P-0013/P-0015).
    app_password: str = ""
    app_session_ttl_seconds: int = 60 * 60 * 24 * 14  # 14 days
    # Mark the session cookie Secure (HTTPS-only). Leave false for plain-http LAN
    # self-hosting (the default); set true when the app is reached over TLS —
    # e.g. behind a cloudflared tunnel or a TLS-terminating reverse proxy — so the
    # session cookie is never sent in cleartext. The app can't auto-detect this
    # because TLS is terminated upstream and the backend speaks plain http.
    cookie_secure: bool = False
    # App-level WebSocket heartbeat. The /ws live feed sends a ping frame on this
    # interval so an idle connection isn't reaped by an upstream proxy during quiet
    # periods (Cloudflare drops idle WebSockets at ~100s). Keep it well under that.
    ws_heartbeat_seconds: int = 30

    @property
    def app_auth_enabled(self) -> bool:
        return bool(self.app_password)

    # ── In-UI console (scoped actions: set models, run auth) ──────────────────
    # Off by default — it execs auth flows inside the container, so it's only
    # safe behind auth and never in managed mode (§ legal guardrail).
    enable_web_console: bool = False
    web_console_token: str = ""  # legacy gate; folded into app-auth when that's on

    # ── Subscription-usage background poll (D-0023, resolves P-0026 b) ─────────
    # Periodically refresh plan-CLI /usage quota so the providers card shows a
    # maintained figure (with an "as of" stamp) instead of a manual-only capture.
    # The full-TTY /usage seam is slow + spends a provider turn, so the cadence is
    # deliberately long; 0 disables the poll (manual refresh still works).
    subscription_usage_poll_seconds: int = 3600  # 1h; 0 = off

    # ── Update check (D-0053) ──────────────────────────────────────────────────
    # The static latest-version.json served by our own public site. The instance
    # reads it to show an inline "update available" hint next to its running
    # version — our CDN, not the GitHub API (no anon rate limits, no instance data
    # sent). Empty string disables the check (version still displays).
    version_check_url: str = "https://batonkeep.com/latest-version.json"
    version_check_ttl_seconds: int = 6 * 3600  # 6h; result cached this long

    @property
    def web_console_available(self) -> bool:
        # The console exists when explicitly enabled and not in managed mode.
        # Its *access* gate is `console_requires_token` below — app-auth folds in
        # the legacy token (D-0023). The managed exec-fence is unconditional.
        return (
            self.enable_web_console
            and self.deployment_mode != DeploymentMode.managed
            and (self.app_auth_enabled or bool(self.web_console_token))
        )

    @property
    def console_requires_token(self) -> bool:
        """True when the legacy X-Console-Token is still the gate.

        With app-auth on, an authenticated operator is already trusted, so the
        console rides the session instead (the token is folded in). With app-auth
        off, fall back to the legacy token so existing deployments and the dev
        path keep working.
        """
        return not self.app_auth_enabled and bool(self.web_console_token)

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:////data/batonkeep.db"

    # ── Outputs ───────────────────────────────────────────────────────────────
    outputs_dir: str = "/data/outputs"

    # ── Agent filesystem isolation (P-0022 / D-0020) ────────────────────────────
    # Privilege separation: the backend runs as `batond`; agent CLIs are launched
    # as the low-privilege `sandbox` user through the setuid spawner, so kernel DAC
    # fences them off from /app and control-plane /data. When the spawner is
    # absent (local dev, tests) the executors fall back to a direct spawn.
    sandbox_spawn_path: str = "/usr/local/bin/sandbox-spawn"
    # Fail-closed switch. When True, any sandboxed launch (agent CLIs, the web-TTY
    # seam, API-path `code_exec`) REFUSES rather than degrading to a direct
    # un-sandboxed spawn when the setuid spawner is unavailable. Set in the
    # container image (`ENV REQUIRE_SANDBOX=1`) so a misbuilt/raced image can never
    # silently run agent or untrusted code as the control-plane `batond` user
    # (the P-0046 non-sandbox bug). Left False in local dev / tests, where no
    # spawner exists and a direct spawn is the intended fallback.
    require_sandbox: bool = False
    # Per-task isolated workspaces: /work/task_<id>/{current,history}. The agent
    # cwd's into current/ (writable); history/ holds prior runs' outputs read-only,
    # promoted by the orchestrator (never the agent) so it can't be poisoned.
    work_dir: str = "/work"
    # The sandbox user's HOME. Some CLI agents (antigravity/agy) save generated
    # artifacts under their own HOME (e.g. /home/agent/.gemini/.../brain/<id>/img.jpg)
    # rather than the run cwd. Task-run asset capture follows references into this
    # root (read via the sandbox helper, since batond can't traverse it) — P-0050.
    sandbox_home: str = "/home/agent"

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

    # ── Web search (P-0046 slice 5) ───────────────────────────────────────────
    # The `web_search` tool prefers a self-hosted SearXNG (key-free, multi-engine
    # JSON API — better recall + robustness than the DDG HTML scrape, benchmarked
    # 2026-06-13). Empty → SearXNG disabled, DDG-scraper-only. The DDG scrape stays
    # the automatic fallback whenever SearXNG is unset/down/empty. Compose sets this
    # to the internal service URL (http://searxng:8080).
    searxng_url: str = ""
    searxng_timeout_seconds: float = 6.0

    # ── Optional API keys (metered / BYO-key providers) ──────────────────────
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None
    xai_api_key: str | None = None

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def candidates_list(self) -> list[str]:
        return [c.strip() for c in self.default_candidates.split(",") if c.strip()]

    @property
    def upload_allowed_ext_set(self) -> set[str]:
        """Lowercased extension allowlist (no leading dots) for asset upload-in."""
        return {
            e.strip().lstrip(".").lower()
            for e in self.upload_allowed_ext.split(",")
            if e.strip()
        }

    @property
    def plan_cli_allowed(self) -> bool:
        """plan-CLI mode is structurally forbidden when DEPLOYMENT_MODE=managed (§1a)."""
        return self.deployment_mode != DeploymentMode.managed

    @model_validator(mode="after")
    def _assert_managed_mode_guardrail(self) -> Settings:
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
