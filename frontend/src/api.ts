// api.ts — typed REST client for the §10 backend. Base path is '/api' (proxied by
// vite in dev, nginx in prod), so the SPA is single-origin and needs no config.

import type {
  Approval,
  ApprovalDecideResult,
  AuthStatus,
  Cockpit,
  ConsoleConfig,
  ContextSource,
  ContextSourcesResult,
  CloudflareConfig,
  CloudflareDeploy,
  CloudflareStatus,
  Credential,
  CustomProvider,
  CustomProviderInput,
  CustomProviderUpdate,
  Evidence,
  FileEntry,
  ImageModel,
  ImportResult,
  Mode,
  ModelPricing,
  Project,
  ProjectInput,
  ProviderCatalog,
  ProviderHealth,
  ProviderLimitsUpdate,
  Run,
  RunAsset,
  RunEvent,
  SecretStatus,
  Session,
  SessionInput,
  SessionTurn,
  SessionUpdate,
  Stats,
  Task,
  TaskInput,
  TaskTemplate,
  TotpSetup,
  TotpStatus,
  TurnInput,
  Publish,
  SessionTemplate,
  UsageSummary,
  Upload,
  Version,
  VersionDiff,
  VersionInfo,
  WorkItem,
  SubtaskItemInput,
  WorkItemInput,
  WorkItemPatchInput,
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
  // ── Meta ───────────────────────────────────────────────────────────────
  // Running version + best-effort latest-release hint (D-0053).
  getVersion: () => req<VersionInfo>("/version"),

  // ── Projects (S0 substrate) ──────────────────────────────────────────────
  listProjects: () => req<Project[]>("/projects"),
  getProject: (id: string) => req<Project>(`/projects/${id}`),
  createProject: (body: ProjectInput) =>
    req<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),
  listWorkItems: (projectId: string, state?: string) =>
    req<WorkItem[]>(`/projects/${projectId}/work-items${state ? `?state=${encodeURIComponent(state)}` : ""}`),
  createWorkItem: (projectId: string, body: WorkItemInput) =>
    req<WorkItem>(`/projects/${projectId}/work-items`, { method: "POST", body: JSON.stringify(body) }),
  updateWorkItem: (itemId: number, body: WorkItemPatchInput) =>
    req<WorkItem>(`/work-items/${itemId}`, { method: "PATCH", body: JSON.stringify(body) }),
  // P-0069 B2: append agent/operator-proposed sub-tasks (status=proposed).
  proposeSubtasks: (itemId: number, items: SubtaskItemInput[], proposedBy = "operator") =>
    req<WorkItem>(`/work-items/${itemId}/subtasks`, {
      method: "POST", body: JSON.stringify({ items, proposed_by: proposedBy }),
    }),
  // Authoritative confirm/modify: replace the checklist with the operator's list.
  setSubtasks: (itemId: number, items: SubtaskItemInput[]) =>
    req<WorkItem>(`/work-items/${itemId}/subtasks`, {
      method: "PUT", body: JSON.stringify({ items }),
    }),
  listContextSources: (projectId: string) =>
    req<ContextSource[]>(`/projects/${projectId}/context-sources`),
  // rel_path null → import every source the project manifest declares.
  declareContextSource: (projectId: string, relPath: string | null) =>
    req<ContextSourcesResult>(`/projects/${projectId}/context-sources`, {
      method: "POST",
      body: JSON.stringify({ rel_path: relPath }),
    }),
  refreshContext: (projectId: string) =>
    req<ContextSource[]>(`/projects/${projectId}/context/refresh`, { method: "POST" }),
  listEvidence: (projectId: string, workItemId?: number) =>
    req<Evidence[]>(
      `/projects/${projectId}/evidence${workItemId != null ? `?work_item_id=${workItemId}` : ""}`
    ),
  evidenceRawUrl: (evidenceId: number) => `${BASE}/evidence/${evidenceId}/raw`,
  // Read-only serving of a declared context source (S0.5): file sources serve
  // directly; dir/git sources take a path naming a file inside the source.
  contextSourceRawUrl: (projectId: string, sourceId: number, path?: string) =>
    `${BASE}/projects/${projectId}/context-sources/${sourceId}/raw` +
    (path ? `?path=${encodeURIComponent(path)}` : ""),
  // Propose a write to the canonical context root — never applies; returns the
  // pending approval carrying the diff. Exactly one of `content` (inline) or
  // `evidence_id` (by-reference promotion, digest-pinned; S0.5).
  proposeCanonicalWrite: (
    projectId: string,
    body: {
      rel_path: string;
      content?: string;
      evidence_id?: number;
      producer?: string;
      work_item_id?: number | null;
    },
  ) =>
    req<Approval>(`/projects/${projectId}/context/propose`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listApprovals: (params?: { status?: string; project_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.project_id) q.set("project_id", params.project_id);
    const qs = q.toString();
    return req<Approval[]>(`/approvals${qs ? `?${qs}` : ""}`);
  },
  // Decide a pending canonical-write proposal (code-exec approvals go through
  // their session route — the backend refuses them here).
  decideApproval: (approvalId: number, approved: boolean) =>
    req<ApprovalDecideResult>(`/approvals/${approvalId}/decide`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),

  // ── Tasks ──────────────────────────────────────────────────────────────
  listTasks: () => req<Task[]>("/tasks"),
  listTaskTemplates: () => req<TaskTemplate[]>("/task-templates"),
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
  // Run assets (P-0050): list + raw URL + delete; clear all for a task.
  listRunAssets: (id: number) => req<RunAsset[]>(`/runs/${id}/assets`),
  runAssetUrl: (id: number, relPath: string, download = false) =>
    `${BASE}/runs/${id}/assets/raw/${relPath.split("/").map(encodeURIComponent).join("/")}${download ? "?download=1" : ""}`,
  // Base for resolving relative asset/data refs embedded in a run's report markdown.
  runAssetBase: (id: number) => `${BASE}/runs/${id}/assets/raw/`,
  deleteRunAssets: (id: number) => req<void>(`/runs/${id}/assets`, { method: "DELETE" }),
  clearTaskAssets: (taskId: number) => req<void>(`/tasks/${taskId}/assets`, { method: "DELETE" }),

  // ── Sessions (M1: build sessions + live preview) ───────────────────────
  listSessionTemplates: () => req<SessionTemplate[]>("/session-templates"),
  listSessions: () => req<Session[]>("/sessions"),
  getSession: (id: string) => req<Session>(`/sessions/${id}`),
  createSession: (body: SessionInput) =>
    req<Session>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  updateSession: (id: string, body: SessionUpdate) =>
    req<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSession: (id: string) => req<void>(`/sessions/${id}`, { method: "DELETE" }),
  // P-0046 slice 3b: resolve a pending code-exec approval.
  resolveApproval: (id: string, requestId: string, approved: boolean) =>
    req<void>(`/sessions/${id}/approvals/${requestId}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),
  listTurns: (id: string) => req<SessionTurn[]>(`/sessions/${id}/turns`),
  createTurn: (id: string, body: TurnInput) =>
    req<SessionTurn>(`/sessions/${id}/turns`, { method: "POST", body: JSON.stringify(body) }),
  // P-0057/D-0051: best-effort interrupt of an in-flight turn. Returns the turn in
  // its resolved (cancelled / already-terminal) state.
  cancelTurn: (id: string, turnId: number) =>
    req<SessionTurn>(`/sessions/${id}/turns/${turnId}/cancel`, { method: "POST" }),
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
  // Workspace packaging (S0.5): capture the tree at git HEAD as an immutable
  // evidence package (zip + MANIFEST.json). Idempotent per commit; the backend
  // 409s while the workspace has uncommitted changes.
  packageWorkspace: (id: string, pinToWorkItemId?: number | null) =>
    req<{
      package: { id: number; rel_path: string };
      manifest: { id: number } | null;
      existing: boolean;
    }>(`/sessions/${id}/package`, {
      method: "POST",
      // pin_to: "hand this artifact to that work item" — the backend appends
      // the package to the target's pinned-evidence inputs atomically.
      body: JSON.stringify({ pin_to_work_item_id: pinToWorkItemId ?? null }),
    }),
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
  listImageModels: () => req<ImageModel[]>("/image-models"),
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
  login: (password: string, totpCode?: string) =>
    req<AuthStatus>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ password, totp_code: totpCode || null }),
    }),
  logout: () => req<AuthStatus>("/auth/logout", { method: "POST" }),
  // TOTP second factor (D-0056). Management routes require a session.
  getTotpStatus: () => req<TotpStatus>("/auth/totp"),
  totpSetup: () => req<TotpSetup>("/auth/totp/setup", { method: "POST" }),
  totpActivate: (code: string) =>
    req<TotpStatus>("/auth/totp/activate", { method: "POST", body: JSON.stringify({ code }) }),
  totpDisable: (code: string) =>
    req<TotpStatus>("/auth/totp/disable", { method: "POST", body: JSON.stringify({ code }) }),
  // Owner-scoped (model-set is no longer console-gated for API providers); the
  // optional token is kept for the ProvidersPanel call sites and is harmless if sent.
  setProviderModel: (
    instanceId: string,
    model: string | null,
    token = "",
    pricing?: { cost_in_per_mtok: number; cost_out_per_mtok: number } | "clear",
  ) => {
    const body: Record<string, unknown> = { model };
    if (pricing === "clear") body.clear_pricing = true;
    else if (pricing) {
      body.cost_in_per_mtok = pricing.cost_in_per_mtok;
      body.cost_out_per_mtok = pricing.cost_out_per_mtok;
    }
    return req<{ status: string; instance: string; model: string | null }>(
      `/providers/${encodeURIComponent(instanceId)}/model`,
      { method: "POST", headers: token ? { "X-Console-Token": token } : {}, body: JSON.stringify(body) }
    );
  },
  // Known-model price lookup so the UI can pre-populate $/Mtok or ask for input.
  getModelPricing: (model: string) =>
    req<ModelPricing>(`/model-pricing?model=${encodeURIComponent(model)}`),
  // P-0049 structured model catalog (per provider template): enabled models +
  // capabilities + pricing + usage, sorted most/recently-used. Powers the picker.
  getProviderCatalog: (template: string) =>
    req<ProviderCatalog>(`/providers/${encodeURIComponent(template)}/catalog`),
  updateCatalogModel: (
    template: string,
    body: {
      id: string; enabled?: boolean; capabilities?: string[];
      cost_in_per_mtok?: number; cost_out_per_mtok?: number; clear_pricing?: boolean;
    },
  ) =>
    req<{ status: string; template: string; model: string }>(
      `/providers/${encodeURIComponent(template)}/catalog/model`,
      { method: "PUT", body: JSON.stringify(body) }
    ),
  setCatalogPreferred: (template: string, capability: string, model: string | null) =>
    req<{ status: string }>(
      `/providers/${encodeURIComponent(template)}/catalog/preferred`,
      { method: "PUT", body: JSON.stringify({ capability, model }) }
    ),
  // Returns the /usage command to pre-fill when the user opens "Check usage → Open terminal"
  // (D-0049). Owner-scoped, no exec, no console token required.
  getProviderUsageCommand: (instanceId: string) =>
    req<{ instance: string; command: string }>(
      `/providers/${encodeURIComponent(instanceId)}/usage-command`
    ),
  // One-shot user-initiated usage capture for the "Capture for me" modal path (D-0049).
  // Console-gated. Returns { output, truncated } with raw uninterpreted terminal output.
  captureUsageRaw: (instanceId: string, token: string) =>
    req<{ instance: string; output: string; truncated: boolean }>(
      `/providers/${encodeURIComponent(instanceId)}/usage-capture`,
      { method: "POST", headers: token ? { "X-Console-Token": token } : {} }
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

  // Built-in provider routing-tag override (P-0044); owner-scoped, like setProviderModel.
  setProviderTags: (providerName: string, capability_tags: string[]) =>
    req<{ status: string; provider: string; capability_tags: string[] }>(
      `/providers/${encodeURIComponent(providerName)}/tags`,
      { method: "POST", body: JSON.stringify({ capability_tags }) }
    ),
  // Operator suspend/reactivate toggle — skip in routing without deleting auth.
  setProviderEnabled: (instanceId: string, enabled: boolean) =>
    req<{ status: string; provider: string; enabled: boolean }>(
      `/providers/${encodeURIComponent(instanceId)}/enabled`,
      { method: "POST", body: JSON.stringify({ enabled }) }
    ),
};

export { ApiError };
