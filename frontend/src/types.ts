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

// ── Sessions (M1: build sessions + live preview) ─────────────────────────────
// Mirrors SessionOut / SessionTurnOut in backend/app/schemas.py.

export type SessionStatus = "active" | "archived";
export type TurnStatus = "running" | "succeeded" | "failed";

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
  created_at: string;
  updated_at: string;
}

// Payload accepted by POST /sessions.
export interface SessionInput {
  title?: string | null;
  goal?: string | null;
  provider?: string | null;
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

// Payload accepted by PATCH /sessions/{id}.
export interface SessionUpdate {
  title?: string | null;
}

// Payload accepted by POST /sessions/{id}/turns.
export interface TurnInput {
  message: string;
  // optional provider switch for this and subsequent turns
  provider?: string | null;
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
