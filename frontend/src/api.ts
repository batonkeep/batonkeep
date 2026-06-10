// api.ts — typed REST client for the §10 backend. Base path is '/api' (proxied by
// vite in dev, nginx in prod), so the SPA is single-origin and needs no config.

import type {
  AuthStatus,
  Cockpit,
  ConsoleConfig,
  CloudflareConfig,
  CloudflareDeploy,
  CloudflareStatus,
  Credential,
  CustomProvider,
  CustomProviderInput,
  CustomProviderUpdate,
  FileEntry,
  ImportResult,
  Mode,
  ProviderHealth,
  ProviderLimitsUpdate,
  Run,
  RunEvent,
  SecretStatus,
  Session,
  SessionInput,
  SessionTurn,
  SessionUpdate,
  Stats,
  Task,
  TaskInput,
  TurnInput,
  Publish,
  SessionTemplate,
  UsageSummary,
  Upload,
  Version,
  VersionDiff,
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
  // FormData (file upload) must set its own multipart boundary — never force JSON.
  const isForm = init?.body instanceof FormData;
  const baseHeaders: Record<string, string> = isForm
    ? {}
    : { "Content-Type": "application/json" };
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    // Send the app-auth session cookie (D-0023). Same-origin in prod; explicit
    // so cross-origin dev (vite proxy bypass) still carries it.
    credentials: "include",
    // Spread init FIRST, then set merged headers LAST — otherwise init's own
    // headers ({"X-Console-Token": ...}) clobber the whole headers object and
    // drop Content-Type: application/json, so a JSON string body is sent as
    // text/plain and FastAPI 422s ("Input should be a valid dictionary").
    headers: { ...baseHeaders, ...(init?.headers || {}) },
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
  listSessionTemplates: () => req<SessionTemplate[]>("/session-templates"),
  listSessions: () => req<Session[]>("/sessions"),
  getSession: (id: string) => req<Session>(`/sessions/${id}`),
  createSession: (body: SessionInput) =>
    req<Session>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  updateSession: (id: string, body: SessionUpdate) =>
    req<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  listTurns: (id: string) => req<SessionTurn[]>(`/sessions/${id}/turns`),
  createTurn: (id: string, body: TurnInput) =>
    req<SessionTurn>(`/sessions/${id}/turns`, { method: "POST", body: JSON.stringify(body) }),
  // D-0017 thread 2: capture the web-TTY terminal lane's workspace edits as a
  // version + artifact turn. Returns the new turn, or null if nothing changed.
  captureTerminal: (id: string, instance?: string) =>
    req<SessionTurn | null>(`/sessions/${id}/capture`, {
      method: "POST",
      body: JSON.stringify({ instance: instance ?? null }),
    }),
  // D-0017 thread 1: the session ledger's auto-maintained cross-provider memory.
  getSummary: (id: string) => req<{ summary: string | null }>(`/sessions/${id}/summary`),
  refreshSummary: (id: string) =>
    req<{ summary: string | null }>(`/sessions/${id}/summary`, { method: "POST" }),
  // Asset upload-in (M1.5): drop files into the session; they land as workspace files
  // (assets/… or data/…) the agent can reference by name. multipart, so no JSON header.
  uploadAssets: (id: string, files: File[]) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return req<Upload>(`/sessions/${id}/uploads`, { method: "POST", headers: {}, body: form });
  },
  // Import an existing site: a .zip / .tar(.gz/.bz2/.xz) extracted into the
  // workspace root, preserving structure (D-0009 follow-on).
  importArchive: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return req<ImportResult>(`/sessions/${id}/import`, { method: "POST", headers: {}, body: form });
  },
  importGit: (id: string, url: string, branch?: string) =>
    req<ImportResult>(`/sessions/${id}/import/git`, {
      method: "POST",
      body: JSON.stringify({ url, branch: branch || null }),
    }),
  // Authenticated live-preview URL for an iframe. The token is a path segment (not a
  // query param) and the base ends in a slash, so the agent's relative asset links
  // (href="style.css") resolve under the same authenticated base and load (M1.2).
  previewUrl: (id: string, token: string, path = "") => {
    const rel = path.replace(/^\/+/, "");
    const base = `${BASE}/sessions/${id}/preview/${encodeURIComponent(token)}`;
    return rel ? `${base}/${rel}` : `${base}/`;
  },
  // Versioning / Undo-History (M1.3): per-turn workspace commits, their diffs, and
  // a checkout-restore that lands the rollback as a new, itself-undoable version.
  listVersions: (id: string) => req<Version[]>(`/sessions/${id}/versions`),
  versionDiff: (id: string, commit: string) =>
    req<VersionDiff>(`/sessions/${id}/versions/${commit}/diff`),
  restoreVersion: (id: string, commit: string) =>
    req<{ commit: string; message: string; restored_from: string }>(
      `/sessions/${id}/restore`,
      { method: "POST", body: JSON.stringify({ commit }) }
    ),
  // Publish + share (M1.4): snapshot the static assets to a revocable public share
  // link (#2), or download them as a zip (#1). getPublish reads current state.
  getPublish: (id: string) => req<Publish>(`/sessions/${id}/publish`),
  publish: (id: string) => req<Publish>(`/sessions/${id}/publish`, { method: "POST" }),
  revokePublish: (id: string) => req<Publish>(`/sessions/${id}/publish`, { method: "DELETE" }),
  // Absolute URL for the public share link / the download zip (anchor hrefs).
  shareUrl: (sharePath: string) => `${window.location.origin}${sharePath}`,
  downloadUrl: (id: string) => `${BASE}/sessions/${id}/download`,

  // Cloudflare Pages host connector (D-0009). Config is owner-level (token stored
  // encrypted on the backend, never returned); deploy is per-session.
  getCloudflare: () => req<CloudflareStatus>("/integrations/cloudflare"),
  setCloudflare: (body: CloudflareConfig) =>
    req<CloudflareStatus>("/integrations/cloudflare", { method: "PUT", body: JSON.stringify(body) }),
  clearCloudflare: () => req<void>("/integrations/cloudflare", { method: "DELETE" }),
  deployCloudflare: (id: string, project_name?: string) =>
    req<CloudflareDeploy>(`/sessions/${id}/publish/cloudflare`, {
      method: "POST",
      body: JSON.stringify({ project_name: project_name || null }),
    }),

  // Session file browser (P-0016 b): list workspace files, and the raw-file route
  // that serves one verbatim. fileRawUrl is the same path agents' rewritten
  // file:// links point at — used here as an anchor href (download) and as the
  // viewer fetch target. getFileContent returns the file as text for the in-pane
  // viewer (syntax-highlighted), so a script opens in Preview instead of navigating.
  listFiles: (id: string) => req<FileEntry[]>(`/sessions/${id}/files`),
  fileRawUrl: (id: string, path: string, download = false) => {
    const rel = path.replace(/^\/+/, "");
    const base = `${BASE}/sessions/${id}/files/raw/${rel}`;
    return download ? `${base}?download=1` : base;
  },
  getFileContent: async (id: string, path: string): Promise<string> => {
    const res = await fetch(api.fileRawUrl(id, path));
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    return res.text();
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

  // App-level auth (D-0023). Status is public; login/logout set/clear the cookie.
  getAuthStatus: () => req<AuthStatus>("/auth/status"),
  login: (password: string) =>
    req<AuthStatus>("/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => req<AuthStatus>("/auth/logout", { method: "POST" }),
  // Owner-scoped (model-set is no longer console-gated for API providers); the
  // optional token is kept for the ProvidersPanel call sites and is harmless if sent.
  setProviderModel: (instanceId: string, model: string | null, token = "") =>
    req<{ status: string; instance: string; model: string | null }>(
      `/providers/${encodeURIComponent(instanceId)}/model`,
      { method: "POST", headers: token ? { "X-Console-Token": token } : {}, body: JSON.stringify({ model }) }
    ),
  // Kicks off a background capture (202); the result lands on the providers list.
  captureSubscriptionUsage: (instanceId: string, token: string) =>
    req<{ status: string; instance: string }>(
      `/usage/subscription/${encodeURIComponent(instanceId)}`,
      { method: "POST", headers: { "X-Console-Token": token } }
    ),

  // ── Stats / meta ─────────────────────────────────────────────────────────
  getStats: () => req<Stats>("/stats"),
  getUsage: () => req<UsageSummary>("/usage"),
  // Operational cockpit (D-0022 Task A) — local-only consolidated telemetry.
  getCockpit: (windowDays = 7) => req<Cockpit>(`/cockpit?window_days=${windowDays}`),
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

  // ── Secrets surface (P-0009 #3): key posture across all providers ─────────
  getSecretsStatus: () => req<SecretStatus[]>("/secrets"),

  // ── Custom providers (D-0026) ─────────────────────────────
  listCustomProviders: () => req<CustomProvider[]>("/custom-providers"),
  createCustomProvider: (body: CustomProviderInput) =>
    req<CustomProvider>("/custom-providers", { method: "POST", body: JSON.stringify(body) }),
  updateCustomProvider: (id: string, body: CustomProviderUpdate) =>
    req<CustomProvider>(`/custom-providers/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteCustomProvider: (id: string) =>
    req<void>(`/custom-providers/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

export { ApiError };
