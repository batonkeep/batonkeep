// ProvidersPanel.tsx — provider cockpit (§12 / §4.4): per plan show logged-in/health,
// cooling-down countdown, an approximate headroom bar (labelled an estimate),
// tier/mode, and a re-auth shortcut.
// D-track: composed from ui/ primitives (Button, Badge, Card, Input, StatusDot).
import { lazy, Suspense, useState } from "react";
import { Check, KeyRound, Pencil, RefreshCw, RotateCcw, Terminal } from "lucide-react";
import type { ProviderHealth } from "../types";
import { api } from "../api";
import { countdown } from "../format";
import { Badge, Button, Card, Input, StatusDot } from "../ui";

const AuthConsole = lazy(() => import("./AuthConsole"));

interface Props {
  providers: ProviderHealth[];
  now: number;
  onRefresh: () => void;
  consoleAvailable: boolean;
  consoleToken: string;
  onSetConsoleToken: (t: string) => void;
}

const TIER_LABEL: Record<string, string> = {
  plan: "plan-CLI", api: "API", open: "open-weight", mock: "mock", frontier: "frontier", agent: "agent",
};

export default function ProvidersPanel({ providers, now, onRefresh, consoleAvailable, consoleToken, onSetConsoleToken }: Props) {
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState("");
  const [authTarget, setAuthTarget] = useState<string | null>(null);

  const handleReset = async (name: string) => { await api.resetProviderCooldown(name); onRefresh(); };

  const saveModel = async (instanceId: string) => {
    try { await api.setProviderModel(instanceId, modelDraft.trim() || null, consoleToken); setEditingModel(null); onRefresh(); }
    catch { /* surfaced via disabled state */ }
  };

  const toggleSeam = async (p: ProviderHealth) => {
    const next = (p.exec_seam ?? "headless") === "terminal" ? "headless" : "terminal";
    try { await api.setProviderSeam(p.name, next, consoleToken); onRefresh(); }
    catch { /* surfaced via disabled state */ }
  };

  const canConsole = consoleAvailable && consoleToken.trim().length > 0;

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

      {/* Console unlock */}
      {consoleAvailable && (
        <Card className="flex items-center gap-2 px-3 py-2">
          <Terminal size={14} className={canConsole ? "text-live" : "text-muted"} />
          <Input
            type="password"
            value={consoleToken}
            onChange={(e) => onSetConsoleToken(e.target.value)}
            placeholder="console token (WEB_CONSOLE_TOKEN) to set models / re-auth"
            className="flex-1 py-1 font-mono text-xs"
          />
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
                            {(p.exec_seam ?? "headless") === "terminal" ? (
                              <Badge tone="defer">
                                terminal seam
                                {canConsole && (
                                  <button onClick={() => toggleSeam(p)} title="switch to headless"
                                    className="ml-1 text-muted hover:text-brand"><Terminal size={10} /></button>
                                )}
                              </Badge>
                            ) : canConsole ? (
                              <button onClick={() => toggleSeam(p)} title="run via PTY terminal seam"
                                className="font-mono text-[10px] text-muted hover:text-brand">
                                headless · use terminal seam
                              </button>
                            ) : (
                              <Badge tone="neutral">headless</Badge>
                            )}
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
                      {cooling ? "cooling" : p.healthy ? "healthy" : "cooling"}
                    </Badge>
                  </div>

                  {(cooling || unhealthy) && (
                    <div className="mt-2 flex items-center justify-between">
                      <span className="font-mono text-xs text-defer">
                        {cooling ? `resets in ${countdown(p.cooldown_until, now)}` : "in cooldown"}
                      </span>
                      <Button variant="outline" size="sm" icon={<RotateCcw size={11} />}
                        onClick={() => handleReset(p.name)}
                        className="border-defer/40 text-defer hover:border-defer">
                        reset
                      </Button>
                    </div>
                  )}

                  <div className="mt-3">
                    <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider text-muted">
                      <span>headroom (est.)</span>
                      <span>{usedPct == null ? "unknown" : `${100 - usedPct}% left`}</span>
                    </div>
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-base">
                      <div className={`h-full ${barColor} transition-all`} style={{ width: `${usedPct == null ? 0 : usedPct}%` }} />
                    </div>
                  </div>

                  {p.mode === "plan" && (
                    <div className="mt-3 border-t border-edge pt-2">
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
                    </div>
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
