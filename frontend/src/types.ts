// types.ts — mirrors backend/app/schemas.py (§10). Keep in sync with the running API.

export type RunStatus =
  | "queued"
  | "planning"
  | "running"
  | "succeeded"
  | "failed"
  | "deferred"
  | "cancelled";

export type EventKind =
  | "log"
  | "phase"
  | "token"
  | "tool"
  | "subagent"
  | "result"
  | "error"
  | "route"
  | "approval";

export type RoutingStrategy = "capability" | "fixed" | "round_robin" | "cost_optimized";

// P-0046 code-exec execution policy.
export type ExecPolicy = "off" | "confirmation" | "allow-safe" | "auto";

export interface RoutingPolicy {
  strategy: RoutingStrategy;
  candidates: string[];
  capability_tags: string[];
  failover: boolean;
  overflow_to: string | null;
  max_attempts: number;
}

export interface Task {
  id: number;
  owner_id: string;
  name: string;
  description: string | null;
  category: string | null;
  prompt_template: string;
  params: Record<string, unknown> | null;
  schedule_kind: "none" | "interval" | "cron";
  schedule_expr: string | null;
  timezone: string; // IANA tz the cron expression is interpreted in
  want_markdown: boolean;
  want_json: boolean;
  enabled: boolean;
  routing: RoutingPolicy | null;
  // P-0046 slice 6: image-gen model override (catalog id; null = provider default).
  image_model_id?: string | null;
  // P-0056/D-0052: per-task run timeout in seconds; null = global default (1800s).
  timeout_seconds?: number | null;
  created_at: string;
  updated_at: string;
}

// Payload accepted by POST/PUT /tasks. `routing` is the full policy object.
export interface TaskInput {
  name: string;
  description?: string | null;
  category?: string | null;
  prompt_template: string;
  params?: Record<string, unknown> | null;
  schedule_kind: "none" | "interval" | "cron";
  schedule_expr?: string | null;
  timezone?: string;
  want_markdown: boolean;
  want_json: boolean;
  enabled: boolean;
  routing?: RoutingPolicy | null;
  // P-0046 slice 6: image-gen model override. "" clears back to provider default.
  image_model_id?: string | null;
  // P-0056/D-0052: per-task run timeout in seconds. null/omitted = global default;
  // -1 (PUT only) clears an existing override back to the default.
  timeout_seconds?: number | null;
}

export interface RunAttempt {
  provider: string;
  outcome: "pending" | "success" | "rate_limited" | "error" | "unavailable";
  reset_at?: string | null;
}

export interface Run {
  id: number;
  owner_id: string;
  task_id: number;
  trigger: "manual" | "schedule";
  status: RunStatus;
  summary: string | null;
  error: string | null;
  provider: string | null;
  model: string | null;
  tier: string | null;
  attempts: RunAttempt[] | null;
  overflow_used: boolean;
  deferred_until: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  subagents: number;
  tool_calls: number;
  markdown_path: string | null;
  json_path: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
}

// A non-text artifact a task run produced (generated image, csv/pdf) — P-0050.
export interface RunAsset {
  id: number;
  run_id: number;
  rel_path: string;
  mime: string | null;
  bytes: number;
  created_at: string;
}

export interface RunEvent {
  id?: number;
  run_id?: number;
  seq: number;
  ts?: string;
  kind: EventKind;
  phase: string | null;
  message: string | null;
  text?: string | null;
  data: Record<string, any> | null;
}

export interface ProviderHealth {
  name: string; // instance id ("claude" or "claude:work")
  template: string; // provider template ("claude") — group instances under this
  label: string; // human display label for the account
  model: string | null; // active model for API instances (null for CLI)
  kind: string;
  tier: string;
  healthy: boolean;
  enabled: boolean; // operator suspend toggle — false = suspended, skipped in routing

  cooldown_until: string | null;
  last_reset_seen: string | null;
  est_used_pct: number | null;
  usage_seen_at: string | null; // when /usage quota was last captured (D-0023 b)
  mode: string; // plan | api | open | mock
  capability_tags: string[]; // effective routing tags (override > template) — P-0044
  cost_in_per_mtok: number; // effective $/Mtok input (override > registry > template)
  cost_out_per_mtok: number; // effective $/Mtok output
  pricing_source: "override" | "registry" | "template";
}

export interface ModelPricing {
  model: string;
  known: boolean;
  cost_in_per_mtok: number | null;
  cost_out_per_mtok: number | null;
}

// P-0049 structured API model catalog (GET /api/providers/{template}/catalog).
export interface CatalogModel {
  id: string;
  enabled: boolean;
  capabilities: string[];
  known: boolean;
  cost_in_per_mtok: number | null;
  cost_out_per_mtok: number | null;
  use_count: number;
  last_used: string | null;
}

export interface ProviderCatalog {
  template: string;
  models: CatalogModel[];
  preferred: Record<string, string>;
  effective_model: string | null;
  capabilities_vocab: string[];
}

export interface ProviderLimitsUpdate {
  window_seconds: number;
  window_limit: number;
}

export interface ConsoleConfig {
  available: boolean;
}

export interface AuthStatus {
  auth_enabled: boolean;
  authenticated: boolean;
  /** TOTP second factor enrolled + active — login needs a code (D-0056). */
  totp_enabled: boolean;
}

export interface TotpStatus {
  enabled: boolean;
  pending: boolean;
  break_glass: boolean;
}

export interface TotpSetup {
  secret: string;
  otpauth_uri: string;
}

export interface Stats {
  runs_today: number;
  success_rate: number;
  avg_duration_ms: number | null;
  runs_by_provider: Record<string, number>;
  failover_rate: number;
  deferred_now: number;
  cost_today_usd: number;
  active_runs: number;
}

export interface Credential {
  id: number;
  owner_id: string;
  provider: string; // template name or instance id ("openai-api" / "openai-api:team")
  label: string | null;
  key_hint: string | null; // non-secret last-4 ("…wxyz"), never the full key
  created_at: string;
  last_used_at: string | null;
}

// Owner spend surface (P-0009 #2). Mirrors UsageSummaryOut in schemas.py.
export interface UsageSummary {
  spend_today_usd: number;
  spend_7d_usd: number;
  by_provider_today: Record<string, number>;
  daily_budget_usd: number; // 0 = unlimited
  remaining_today_usd: number | null; // null when unlimited
  over_budget: boolean;
}

// Operational cockpit (D-0022 Task A, audience A). Mirrors CockpitOut in
// backend/app/schemas.py. Local-first and sovereign — nothing here is shared.
export interface Cockpit {
  window_days: number;
  since: string;
  generated_at: string;
  spend: UsageSummary;
  runs: {
    total: number;
    by_status: Record<string, number>;
    by_provider: Record<string, number>;
    by_trigger: Record<string, number>;
    success_rate: number;
    error_rate: number;
    deferred_now: number;
    active_runs: number;
  };
  latency: {
    avg_ms: number | null;
    p50_ms: number | null;
    p95_ms: number | null;
    sample: number;
  };
  reliability: {
    failover_rate: number;
    failover_reasons: Record<string, number>;
    retried_runs: number;
    budget_degraded_runs: number;
  };
  errors_by_class: Record<string, number>;
  activity: {
    sessions_total: number;
    sessions_active: number;
    sessions_archived: number;
    sessions_confidential: number;
    turns_total: number;
    turns_by_status: Record<string, number>;
  };
}

// One row of the named secrets-management surface (P-0009 #3). Mirrors
// SecretStatusOut in backend/app/schemas.py. Never carries any plaintext.
export interface SecretStatus {
  provider: string;
  tier: string;
  kind: string; // "openai_compatible" | "anthropic" | "gemini"
  env_key: string | null;
  local: boolean;
  source: "stored" | "env" | "missing";
  key_hint: string | null;
  model: string | null;
  last_used_at: string | null;
}

export interface Mode {
  mode: "plan" | "byo_key" | "hosted";
  deployment_mode: "personal" | "oss" | "managed";
  plan_cli_allowed: boolean;
}

// ── Sessions (M1: build sessions + live preview) ─────────────────────────────
// Mirrors SessionOut / SessionTurnOut in backend/app/schemas.py.

export type SessionStatus = "active" | "archived";
export type TurnStatus = "running" | "succeeded" | "failed" | "cancelled";

export interface Session {
  id: string;
  owner_id: string;
  title: string;
  // currently-selected provider instance id (e.g. "grok", "agy", "mock")
  provider: string | null;
  workspace_path: string;
  // unguessable token that gates the live preview (M1.2).
  preview_token: string;
  status: SessionStatus;
  // Cloudflare Pages project this session deploys to (D-0009); null until first deploy.
  cf_project?: string | null;
  // P-0009 #1: pinned to a local model — prompt + workspace never leave the box.
  confidential: boolean;
  // P-0046: code-exec execution policy.
  exec_policy: ExecPolicy;
  // P-0049: per-session model override for the API provider (null = catalog default).
  model?: string | null;
  // P-0046 slice 6: image-gen model override (catalog id; null = provider default).
  image_model_id?: string | null;
  // Optional per-session spend cap (USD, API path); null = no cap (opt-in).
  budget_usd?: number | null;
  // Cumulative session spend (sum of succeeded turns), for the live cost surface.
  cost_usd?: number;
  // Content signals (from the list endpoint) used to scale delete confirmation.
  turn_count?: number;
  published?: boolean;
  created_at: string;
  updated_at: string;
}

// Payload accepted by POST /sessions.
export interface SessionInput {
  title?: string | null;
  goal?: string | null;
  provider?: string | null;
  template?: string | null;
  confidential?: boolean;
  model?: string | null;
  image_model_id?: string | null;
}

// A selectable image-generation model (P-0046 slice 6). `available` is false when
// the model's home provider has no usable credential.
export interface ImageModel {
  id: string;
  label: string;
  provider: string;
  model: string;
  cost_per_image: number;
  cost_per_mtok: number;
  available: boolean;
}

// One file a turn produced (D-0017 thread 2). status ∈ added/changed/removed;
// additions/deletions are null for binary files.
export interface FileChange {
  path: string;
  status: string;
  additions: number | null;
  deletions: number | null;
}

export interface SessionTurn {
  id: number;
  session_id: string;
  seq: number;
  provider: string | null;
  prompt: string;
  response: string | null;
  status: TurnStatus;
  error: string | null;
  // M1.3 versioning: the workspace commit this turn produced (if any) + summary.
  commit_sha: string | null;
  diffstat: string | null;
  // D-0017 thread 2: the per-file artifacts this turn produced (the headline
  // result surfaced to the user, above any scraped agent text).
  changed_files: FileChange[] | null;
  // Per-turn token/cost usage (API path).
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  created_at: string;
  finished_at: string | null;
}

// One workspace version (commit) — the Undo/History list (M1.3).
export interface Version {
  commit: string;
  short: string;
  ts: string;
  message: string;
}

// The diff a single version introduced (M1.3).
export interface VersionDiff {
  commit: string;
  diffstat: string;
  diff: string;
  files: FileChange[];
}

// Publish/share state of a session's build (M1.4).
export interface Publish {
  published: boolean;
  share_token: string | null;
  share_path: string | null; // e.g. "/api/share/<token>/"
  version: string | null;
  kind: string;
  file_count: number | null;
  updated_at: string | null;
}

// Result of POST /sessions/{id}/uploads (M1.5): workspace-relative paths the agent
// can reference by name, plus the version (commit) the upload produced.
export interface Upload {
  paths: string[];
  commit_sha: string | null;
}

// Result of importing an existing site (zip/tar) into a session.
export interface ImportResult {
  paths: string[];
  count: number;
  commit_sha: string | null;
}

// One workspace file in the session file browser (P-0016 b).
export interface FileEntry {
  path: string;
  size: number;
  modified: number;
}

// Cloudflare Pages host connector (D-0009). Token + account are owner-level;
// the project is per-session (passed at deploy time).
export interface CloudflareConfig {
  api_token: string;
  account_id: string;
}
export interface CloudflareStatus {
  configured: boolean;
  account_id?: string | null;
}
export interface CloudflareDeploy {
  url: string;
  project: string;
}

// A session task type (P-0010 / D-0011) offered as a starter card.
export interface SessionTemplate {
  id: string;
  label: string;
  description: string;
}

// Starter task preset offered on a fresh install. `input` (seeded enabled=false)
// pre-fills the task form; nothing is persisted until the user saves.
export interface TaskTemplate {
  id: string;
  label: string;
  description: string;
  input: TaskInput;
}

// Payload accepted by PATCH /sessions/{id}.
export interface SessionUpdate {
  title?: string | null;
  confidential?: boolean;
  exec_policy?: ExecPolicy;
  // P-0049: per-session model override (API path). "" clears to the provider default.
  model?: string | null;
  // P-0046 slice 6: image-gen model override. "" clears back to provider default.
  image_model_id?: string | null;
  // Per-session spend cap (USD). Positive sets/raises the cap; 0 clears it.
  budget_usd?: number;
}

// Payload accepted by POST /sessions/{id}/turns.
export interface TurnInput {
  message: string;
  // optional provider switch for this and subsequent turns
  provider?: string | null;
  // P-0049: optional model switch (API path). "" clears back to the provider default.
  model?: string | null;
}

// ── WebSocket frames (ws.py / §10) ──────────────────────────────────────────

export interface WsRunUpdate {
  type: "run.update";
  run: Run;
}

export interface WsRunEvent {
  type: "run.event";
  run_id: number;
  event: RunEvent;
}

// Session live frames (orchestrator._broadcast_turn / _broadcast_event).
export interface WsSessionTurnUpdate {
  type: "session.turn.update";
  session_id: string;
  turn: {
    id: number;
    seq: number;
    provider: string | null;
    status: TurnStatus;
    switched?: boolean;
  };
}

export interface WsSessionEvent {
  type: "session.event";
  session_id: string;
  turn_id: number;
  turn_seq: number;
  event: {
    kind: EventKind;
    message: string | null;
    text: string | null;
    phase: string | null;
    data: Record<string, any> | null;
  };
}

export type WsMessage =
  | WsRunUpdate
  | WsRunEvent
  | WsSessionTurnUpdate
  | WsSessionEvent;

// ── Custom providers (D-0026) ──────────────────────────────────────────────
// Operator-defined local/Ollama/open-API endpoints. Mirrors CustomProviderOut.

export type CustomProviderAuthType = "none" | "bearer" | "api_key_header";

export interface CustomProvider {
  id: string;
  label: string;
  base_url: string;
  default_model: string;
  auth_type: CustomProviderAuthType;
  env_key: string | null;
  local: boolean;
  enabled: boolean;
  extra_models: string;
  capability_tags: string[];
  cost_in_per_mtok: number;
  cost_out_per_mtok: number;
}

export interface CustomProviderInput {
  id: string;
  label: string;
  base_url: string;
  default_model: string;
  auth_type?: CustomProviderAuthType;
  env_key?: string | null;
  local?: boolean;
  extra_models?: string;
  capability_tags?: string[];
  cost_in_per_mtok?: number;
  cost_out_per_mtok?: number;
}

export interface CustomProviderUpdate {
  label?: string | null;
  base_url?: string | null;
  default_model?: string | null;
  auth_type?: CustomProviderAuthType | null;
  env_key?: string | null;
  local?: boolean | null;
  enabled?: boolean | null;
  extra_models?: string | null;
  capability_tags?: string[] | null;
  cost_in_per_mtok?: number | null;
  cost_out_per_mtok?: number | null;
}

// Running version + best-effort latest-release hint (D-0053). `latest`/`release_url`
// are null when the update check is disabled or unreachable.
export interface VersionInfo {
  version: string;
  latest: string | null;
  update_available: boolean;
  release_url: string | null;
}
