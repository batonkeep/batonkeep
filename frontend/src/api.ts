// api.ts — typed REST client for the §10 backend. Base path is '/api' (proxied by
// vite in dev, nginx in prod), so the SPA is single-origin and needs no config.

import type {
  ConsoleConfig,
  Credential,
  Mode,
  ProviderHealth,
  ProviderLimitsUpdate,
  Run,
  RunEvent,
  Session,
  SessionInput,
  SessionTurn,
  SessionUpdate,
  Stats,
  Task,
  TaskInput,
  TurnInput,
} from "./types";

const BASE = "/api";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  // ── Tasks ──────────────────────────────────────────────────────────────
  listTasks: () => req<Task[]>("/tasks"),
  getTask: (id: number) => req<Task>(`/tasks/${id}`),
  createTask: (body: TaskInput) =>
    req<Task>("/tasks", { method: "POST", body: JSON.stringify(body) }),
  updateTask: (id: number, body: Partial<TaskInput>) =>
    req<Task>(`/tasks/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTask: (id: number) => req<void>(`/tasks/${id}`, { method: "DELETE" }),
  runTask: (id: number) =>
    req<Run>(`/tasks/${id}/runs`, { method: "POST" }),

  // ── Runs ───────────────────────────────────────────────────────────────
  listRuns: (params?: { task_id?: number; status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.task_id != null) q.set("task_id", String(params.task_id));
    if (params?.status) q.set("status", params.status);
    if (params?.limit != null) q.set("limit", String(params.limit));
    const qs = q.toString();
    return req<Run[]>(`/runs${qs ? `?${qs}` : ""}`);
  },
  getRun: (id: number) => req<Run>(`/runs/${id}`),
  getRunEvents: (id: number) => req<RunEvent[]>(`/runs/${id}/events`),
  cancelRun: (id: number) => req<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  requeueRun: (id: number) => req<Run>(`/runs/${id}/requeue`, { method: "POST" }),
  outputUrl: (id: number, format: "md" | "json") =>
    `${BASE}/runs/${id}/output?format=${format}`,

  // ── Sessions (M1: build sessions + live preview) ───────────────────────
  listSessions: () => req<Session[]>("/sessions"),
  getSession: (id: string) => req<Session>(`/sessions/${id}`),
  createSession: (body: SessionInput) =>
    req<Session>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  updateSession: (id: string, body: SessionUpdate) =>
    req<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  listTurns: (id: string) => req<SessionTurn[]>(`/sessions/${id}/turns`),
  createTurn: (id: string, body: TurnInput) =>
    req<SessionTurn>(`/sessions/${id}/turns`, { method: "POST", body: JSON.stringify(body) }),
  // Authenticated live-preview URL for an iframe. The token is a path segment (not a
  // query param) and the base ends in a slash, so the agent's relative asset links
  // (href="style.css") resolve under the same authenticated base and load (M1.2).
  previewUrl: (id: string, token: string, path = "") => {
    const rel = path.replace(/^\/+/, "");
    const base = `${BASE}/sessions/${id}/preview/${encodeURIComponent(token)}`;
    return rel ? `${base}/${rel}` : `${base}/`;
  },

  // ── Providers ──────────────────────────────────────────────────────────
  listProviders: () => req<ProviderHealth[]>("/providers"),
  setProviderLimits: (name: string, body: ProviderLimitsUpdate) =>
    req<{ status: string }>(`/providers/${name}/limits`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  resetProviderCooldown: (name: string) =>
    req<{ status: string; provider: string }>(`/providers/${name}/reset`, { method: "POST" }),

  // ── Console (scoped actions: set model, run auth) ──────────────────────────
  getConsoleConfig: () => req<ConsoleConfig>("/console/config"),
  setProviderModel: (instanceId: string, model: string | null, token: string) =>
    req<{ status: string; instance: string; model: string | null }>(
      `/providers/${encodeURIComponent(instanceId)}/model`,
      { method: "POST", headers: { "X-Console-Token": token }, body: JSON.stringify({ model }) }
    ),

  // ── Stats / meta ─────────────────────────────────────────────────────────
  getStats: () => req<Stats>("/stats"),
  getMode: () => req<Mode>("/me/mode"),

  // ── Credentials (BYO-key) ─────────────────────────────────────────────────
  listCredentials: () => req<Credential[]>("/credentials"),
  createCredential: (provider: string, api_key: string, label?: string | null) =>
    req<Credential>("/credentials", {
      method: "POST",
      body: JSON.stringify({ provider, api_key, label: label ?? null }),
    }),
  deleteCredential: (provider: string) =>
    req<void>(`/credentials/${provider}`, { method: "DELETE" }),
};

export { ApiError };
