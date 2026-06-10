// ProvidersPanel.tsx — provider cockpit (§12 / §4.4): per plan show logged-in/health,
// cooling-down countdown, an approximate headroom bar (labelled an estimate),
// tier/mode, and a re-auth shortcut.
// D-track: composed from ui/ primitives (Button, Badge, Card, Input, StatusDot).
// D-0026: custom provider cards + Add/Edit/Delete at bottom of the list.
import { lazy, Suspense, useEffect, useState } from "react";
import { Check, KeyRound, Pencil, Plus, RefreshCw, RotateCcw, ShieldCheck, Terminal, Trash2 } from "lucide-react";
import type { CustomProvider, ProviderHealth } from "../types";
import { api } from "../api";
import { countdown, fmtRelative } from "../format";
import { Badge, Button, Card, Input, StatusDot } from "../ui";
import CustomProviderForm from "./CustomProviderForm";
import TagEditor from "./TagEditor";

const AuthConsole = lazy(() => import("./AuthConsole"));

// A labelled section inside a provider card — the auth / state / usage zones.
function Zone({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mt-3 border-t border-edge pt-2">
      <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted/70">{label}</div>
      {children}
    </div>
  );
}

interface Props {
  providers: ProviderHealth[];
  now: number;
  onRefresh: () => void;
  consoleAvailable: boolean;
  consoleToken: string;
  onSetConsoleToken: (t: string) => void;
  // When app-auth is on, the legacy console token is folded into the session
  // (D-0023): an authenticated operator is trusted, so no token entry is shown.
  appAuthEnabled: boolean;
}

const TIER_LABEL: Record<string, string> = {
  plan: "plan-CLI", api: "API", open: "open-weight", mock: "mock", frontier: "frontier", agent: "agent",
};

export default function ProvidersPanel({ providers, now, onRefresh, consoleAvailable, consoleToken, onSetConsoleToken, appAuthEnabled }: Props) {
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState("");
  const [editingTags, setEditingTags] = useState<string | null>(null);
  const [tagsDraft, setTagsDraft] = useState<string[]>([]);
  const [authTarget, setAuthTarget] = useState<string | null>(null);

  // ── Custom providers (D-0026) ──────────────────────────────────────────────
  const [customProviders, setCustomProviders] = useState<CustomProvider[]>([]);
  const [showAddForm, setShowAddForm] = useState(false);
  const [editingCustom, setEditingCustom] = useState<CustomProvider | null>(null);
  const [deletingCustom, setDeletingCustom] = useState<string | null>(null);

  const loadCustomProviders = () =>
    api.listCustomProviders().then(setCustomProviders).catch(() => {});

  useEffect(() => { loadCustomProviders(); }, []);

  const handleCustomSaved = () => {
    setShowAddForm(false);
    setEditingCustom(null);
    loadCustomProviders();
    onRefresh(); // re-fetch /providers health so new entry appears in the list
  };

  const handleDeleteCustom = async (id: string) => {
    setDeletingCustom(id);
    try {
      await api.deleteCustomProvider(id);
      await loadCustomProviders();
      onRefresh();
    } catch { /* ignore */ }
    finally { setDeletingCustom(null); }
  };

  const handleReset = async (name: string) => { await api.resetProviderCooldown(name); onRefresh(); };

  const saveModel = async (instanceId: string) => {
    try { await api.setProviderModel(instanceId, modelDraft.trim() || null, consoleToken); setEditingModel(null); onRefresh(); }
    catch { /* surfaced via disabled state */ }
  };

  const saveTags = async (providerName: string) => {
    try { await api.setProviderTags(providerName, tagsDraft); setEditingTags(null); onRefresh(); }
    catch { /* surfaced via disabled state */ }
  };

  const [capturing, setCapturing] = useState<string | null>(null);
  const captureUsage = async (instanceId: string) => {
    setCapturing(instanceId);
    // The capture now runs server-side in the background (the full-TTY /usage drive
    // is slow — a synchronous wait tripped a gateway 504, esp. for grok). Trigger it,
    // then poll until the freshness stamp advances (the 5s providers refresh updates
    // the bar in the meantime). Bounded so a stuck capture eventually releases the UI.
    const before = providers.find((p) => p.name === instanceId)?.usage_seen_at ?? null;
    try {
      await api.captureSubscriptionUsage(instanceId, consoleToken);
      for (let i = 0; i < 24; i++) {
        await new Promise((r) => setTimeout(r, 2500));
        const list = await api.listProviders().catch(() => null);
        const seen = list?.find((p) => p.name === instanceId)?.usage_seen_at ?? null;
        if (seen && seen !== before) break;
      }
    } catch { /* trigger failed (e.g. not authorized) */ }
    finally { onRefresh(); setCapturing(null); }
  };

  // With app-auth the session is the gate; otherwise the legacy token is required.
  const canConsole = consoleAvailable && (appAuthEnabled || consoleToken.trim().length > 0);

  const groups: { template: string; instances: ProviderHealth[] }[] = [];
  for (const p of providers) {
    let g = groups.find((x) => x.template === p.template);
    if (!g) { g = { template: p.template, instances: [] }; groups.push(g); }
    g.instances.push(p);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted">
          Which plans have capacity right now. Headroom is an{" "}
          <span className="text-defer">estimate</span> — the reliable guarantee is failover on observed limits.
        </p>
        <Button variant="outline" size="sm" icon={<RefreshCw size={13} />} onClick={onRefresh}>
          Refresh
        </Button>
      </div>

      {/* Console unlock. With app-auth the session unlocks it (no token entry);
          otherwise the legacy WEB_CONSOLE_TOKEN gates model-set / re-auth. */}
      {consoleAvailable && (
        <Card className="flex items-center gap-2 px-3 py-2">
          <Terminal size={14} className={canConsole ? "text-live" : "text-muted"} />
          {appAuthEnabled ? (
            <span className="flex-1 text-xs text-muted">
              Console actions (set model · re-auth) are unlocked for your signed-in session.
            </span>
          ) : (
            <Input
              type="password"
              value={consoleToken}
              onChange={(e) => onSetConsoleToken(e.target.value)}
              placeholder="console token (WEB_CONSOLE_TOKEN) to set models / re-auth"
              className="flex-1 py-1 font-mono text-xs"
            />
          )}
          <Badge tone={canConsole ? "ok" : "neutral"}>{canConsole ? "unlocked" : "locked"}</Badge>
        </Card>
      )}

      {groups.map((group) => (
        <div key={group.template} className="space-y-2">
          {group.instances.length > 1 && (
            <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider text-muted">
              <span className="text-ink">{group.template}</span>
              <Badge tone="neutral">{group.instances.length} accounts</Badge>
            </div>
          )}
          <div className="stagger grid grid-cols-1 gap-3 md:grid-cols-2">
            {group.instances.map((p) => {
              const isExtra = p.name !== p.template;
              const cooling = !!p.cooldown_until && new Date(p.cooldown_until).getTime() > now;
              const usedPct = p.est_used_pct != null ? Math.round(p.est_used_pct * 100) : null;
              // The bar fills with *remaining headroom* so a healthy plan reads full,
              // and drains/reddens as the quota is consumed.
              const headroomPct = usedPct == null ? null : 100 - usedPct;
              const barColor = headroomPct == null ? "bg-edge"
                : headroomPct < 15 ? "bg-bad" : headroomPct < 40 ? "bg-defer" : "bg-brand";
              const healthTone = cooling ? "defer" : p.healthy ? "ok" : "bad";

              return (
                <Card key={p.name} className="p-4">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <StatusDot tone={healthTone} pulse={cooling} />
                        <span className="font-mono text-sm font-semibold text-ink">{p.label || p.name}</span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        {isExtra && <Badge tone="neutral">{p.name}</Badge>}
                        <Badge tone="neutral">{TIER_LABEL[p.tier] || p.tier}</Badge>
                        <Badge tone="neutral">{p.mode}</Badge>
                        {p.kind === "cli" ? (
                          <>
                            <Badge tone="neutral">{p.model || "CLI default"}</Badge>
                            <Badge tone="neutral">headless</Badge>
                          </>
                        ) : editingModel === p.name ? (
                          <span className="flex items-center gap-1">
                            <input autoFocus value={modelDraft} onChange={(e) => setModelDraft(e.target.value)}
                              onKeyDown={(e) => e.key === "Enter" && saveModel(p.name)}
                              placeholder={p.model || "model id"}
                              className="w-32 rounded border border-brand/50 bg-base px-1.5 py-0.5 font-mono text-[10px] text-ink outline-none"
                            />
                            <button onClick={() => saveModel(p.name)} className="text-ok hover:text-ink"><Check size={12} /></button>
                          </span>
                        ) : (
                          <Badge tone="neutral">
                            {p.model || "default model"}
                            {canConsole && p.kind !== "cli" && (
                              <button onClick={() => { setEditingModel(p.name); setModelDraft(p.model || ""); }}
                                className="ml-1 text-muted hover:text-brand"><Pencil size={10} /></button>
                            )}
                          </Badge>
                        )}
                      </div>
                    </div>
                    <Badge tone={healthTone}>
                      {cooling ? "cooling" : p.healthy ? "healthy" : "offline"}
                    </Badge>
                  </div>

                  {/* Cooldown detail + reset — only when there's something actionable.
                      The at-a-glance state lives in the header badge (not repeated here). */}
                  {cooling && (
                    <div className="mt-3 flex items-center justify-between border-t border-edge pt-2">
                      <span className="font-mono text-xs text-defer">
                        resets in {countdown(p.cooldown_until, now)}
                      </span>
                      <Button variant="outline" size="sm" icon={<RotateCcw size={11} />}
                        onClick={() => handleReset(p.name)}
                        className="border-defer/40 text-defer hover:border-defer">
                        reset
                      </Button>
                    </div>
                  )}

                  {/* USAGE zone — headroom estimate + freshness + manual refresh */}
                  <Zone label="usage">
                    <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider text-muted">
                      <span>headroom (est.)</span>
                      <span>{headroomPct == null ? "unknown" : `${headroomPct}% left`}</span>
                    </div>
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-base">
                      <div className={`h-full ${barColor} transition-all`} style={{ width: `${headroomPct == null ? 0 : headroomPct}%` }} />
                    </div>
                    {p.kind === "cli" && (
                      <div className="mt-1.5 flex items-center justify-between">
                        <span className="font-mono text-[10px] text-muted">
                          {p.usage_seen_at ? `as of ${fmtRelative(p.usage_seen_at)}` : "not yet captured"}
                        </span>
                        {canConsole && (
                          <button onClick={() => captureUsage(p.name)} disabled={capturing === p.name}
                            title="Drive /usage once via the full-TTY single-shot seam (read-only). Auto-refreshed in the background; this forces it now."
                            className="flex items-center gap-1 font-mono text-[10px] text-brand hover:text-ink disabled:text-muted">
                            <RefreshCw size={10} className={capturing === p.name ? "animate-spin" : ""} />
                            {capturing === p.name ? "capturing…" : "refresh"}
                          </button>
                        )}
                      </div>
                    )}
                  </Zone>

                  {/* ROUTING zone — capability tags (which tasks route here) + edit */}
                  <Zone label="routing">
                    {editingTags === p.name ? (
                      <div className="space-y-2">
                        <TagEditor value={tagsDraft} onChange={setTagsDraft} />
                        <div className="flex items-center gap-2">
                          <button onClick={() => saveTags(p.name)}
                            className="flex items-center gap-1 font-mono text-[11px] text-ok hover:text-ink">
                            <Check size={12} /> save tags
                          </button>
                          <button onClick={() => setEditingTags(null)}
                            className="font-mono text-[11px] text-muted hover:text-ink">cancel</button>
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex min-w-0 flex-wrap items-center gap-1">
                          {p.capability_tags.length === 0
                            ? <span className="font-mono text-[10px] text-muted">no tags</span>
                            : p.capability_tags.map((t) => (
                              <Badge key={t} tone="neutral">{t}</Badge>
                            ))}
                        </div>
                        <button onClick={() => { setEditingTags(p.name); setTagsDraft(p.capability_tags); }}
                          title="Set which task capability-tags route to this provider"
                          className="shrink-0 text-muted hover:text-brand"><Pencil size={11} /></button>
                      </div>
                    )}
                  </Zone>

                  {/* AUTH zone — re-auth path (plan-CLI only) */}
                  {p.mode === "plan" && (
                    <Zone label="auth">
                      {canConsole ? (
                        <button onClick={() => setAuthTarget(p.name)}
                          className="flex items-center gap-1.5 font-mono text-[11px] text-brand hover:text-ink">
                          <Terminal size={12} /> re-auth in console
                        </button>
                      ) : (
                        <span className="flex items-center gap-1.5 font-mono text-[11px] text-muted">
                          <KeyRound size={12} /> re-auth via `make auth p={p.name}`
                        </span>
                      )}
                    </Zone>
                  )}
                </Card>
              );
            })}
          </div>
        </div>
      ))}

      {/* ── Custom providers (D-0026) ─────────────────────────────────── */}
      {(customProviders.length > 0 || showAddForm || editingCustom) && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted">Custom / local</span>
            {customProviders.length > 0 && (
              <Badge tone="neutral">{customProviders.length}</Badge>
            )}
          </div>

          <div className="space-y-2">
            {customProviders.map((cp) => (
              editingCustom?.id === cp.id ? (
                <CustomProviderForm
                  key={cp.id}
                  existing={cp}
                  onSaved={handleCustomSaved}
                  onCancel={() => setEditingCustom(null)}
                />
              ) : (
                <Card key={cp.id} className="p-3 flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      {cp.local && <ShieldCheck size={12} className="shrink-0 text-teal-400" />}
                      <span className="font-mono text-sm font-semibold text-ink truncate">{cp.label}</span>
                      {!cp.enabled && <Badge tone="neutral">disabled</Badge>}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <Badge tone="neutral">custom</Badge>
                      {cp.local && <Badge tone="ok">local</Badge>}
                      <Badge tone="neutral">{cp.default_model}</Badge>
                      <span className="font-mono text-[10px] text-muted truncate max-w-[180px]">
                        {cp.base_url}
                      </span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      onClick={() => { setEditingCustom(cp); setShowAddForm(false); }}
                      className="rounded p-1.5 text-muted hover:text-brand transition-colors"
                      title="Edit"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={() => handleDeleteCustom(cp.id)}
                      disabled={deletingCustom === cp.id}
                      className="rounded p-1.5 text-muted hover:text-bad transition-colors disabled:opacity-40"
                      title="Delete"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </Card>
              )
            ))}
          </div>
        </div>
      )}

      {/* ── Add form (create mode) ─────────────────────────────────────── */}
      {showAddForm && !editingCustom && (
        <CustomProviderForm
          onSaved={handleCustomSaved}
          onCancel={() => setShowAddForm(false)}
        />
      )}

      {/* ── Add custom provider button ─────────────────────────────────── */}
      {!showAddForm && !editingCustom && (
        <button
          type="button"
          onClick={() => setShowAddForm(true)}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-edge py-2.5 text-sm text-muted transition-colors hover:border-brand/50 hover:text-brand"
        >
          <Plus size={14} />
          Add local / custom API
        </button>
      )}

      {authTarget && (
        <Suspense fallback={null}>
          <AuthConsole target={authTarget} token={consoleToken} onClose={() => { setAuthTarget(null); onRefresh(); }} />
        </Suspense>
      )}
    </div>
  );
}
