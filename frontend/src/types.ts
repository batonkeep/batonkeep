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
  | "route";

export type RoutingStrategy = "capability" | "fixed" | "round_robin" | "cost_optimized";

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
  cooldown_until: string | null;
  last_reset_seen: string | null;
  est_used_pct: number | null;
  mode: string; // plan | api | open | mock
}

export interface ProviderLimitsUpdate {
  window_seconds: number;
  window_limit: number;
}

export interface ConsoleConfig {
  available: boolean;
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
  created_at: string;
}

export interface Mode {
  mode: "plan" | "byo_key" | "hosted";
  deployment_mode: "personal" | "oss" | "managed";
  plan_cli_allowed: boolean;
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

export type WsMessage = WsRunUpdate | WsRunEvent;
