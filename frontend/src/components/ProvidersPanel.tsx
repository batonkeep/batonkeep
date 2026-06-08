// ProvidersPanel.tsx — provider cockpit (§12 / §4.4): per plan show logged-in/health,
// cooling-down countdown, an approximate headroom bar (labelled an estimate),
// tier/mode, and a re-auth shortcut.
// D-track: composed from ui/ primitives (Button, Badge, Card, Input, StatusDot).
import { lazy, Suspense, useState } from "react";
import { Check, KeyRound, Pencil, RefreshCw, RotateCcw, Terminal } from "lucide-react";
import type { ProviderHealth } from "../types";
import { api } from "../api";
import { countdown, fmtRelative } from "../format";
import { Badge, Button, Card, Input, StatusDot } from "../ui";

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
  const [authTarget, setAuthTarget] = useState<string | null>(null);

  const handleReset = async (name: string) => { await api.resetProviderCooldown(name); onRefresh(); };

  const saveModel = async (instanceId: string) => {
    try { await api.setProviderModel(instanceId, modelDraft.trim() || null, consoleToken); setEditingModel(null); onRefresh(); }
    catch { /* surfaced via disabled state */ }
  };

  const [capturing, setCapturing] = useState<string | null>(null);
  const captureUsage = async (instanceId: string) => {
    setCapturing(instanceId);
    try { await api.captureSubscriptionUsage(instanceId, consoleToken); onRefresh(); }
    catch { /* surfaced via disabled state */ }
    finally { setCapturing(null); }
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
              const unhealthy = !p.healthy;
              const usedPct = p.est_used_pct != null ? Math.round(p.est_used_pct * 100) : null;
              const barColor = usedPct == null ? "bg-edge" : usedPct > 85 ? "bg-bad" : usedPct > 60 ? "bg-defer" : "bg-brand";
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

                  {/* STATE zone — health detail + cooldown reset */}
                  <Zone label="state">
                    <div className="flex items-center justify-between">
                      <span className={`font-mono text-xs ${cooling ? "text-defer" : unhealthy ? "text-bad" : "text-ok"}`}>
                        {cooling
                          ? `cooling — resets in ${countdown(p.cooldown_until, now)}`
                          : unhealthy ? "offline — not connected" : "healthy — ready"}
                      </span>
                      {cooling && (
                        <Button variant="outline" size="sm" icon={<RotateCcw size={11} />}
                          onClick={() => handleReset(p.name)}
                          className="border-defer/40 text-defer hover:border-defer">
                          reset
                        </Button>
                      )}
                    </div>
                  </Zone>

                  {/* USAGE zone — headroom estimate + freshness + manual refresh */}
                  <Zone label="usage">
                    <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider text-muted">
                      <span>headroom (est.)</span>
                      <span>{usedPct == null ? "unknown" : `${100 - usedPct}% left`}</span>
                    </div>
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-base">
                      <div className={`h-full ${barColor} transition-all`} style={{ width: `${usedPct == null ? 0 : usedPct}%` }} />
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

      {authTarget && (
        <Suspense fallback={null}>
          <AuthConsole target={authTarget} token={consoleToken} onClose={() => { setAuthTarget(null); onRefresh(); }} />
        </Suspense>
      )}
    </div>
  );
}
