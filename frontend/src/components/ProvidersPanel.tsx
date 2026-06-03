// ProvidersPanel.tsx — provider cockpit (§12 / §4.4): per plan show logged-in/health,
// cooling-down countdown, an approximate headroom bar (labelled an estimate),
// tier/mode, and a re-auth shortcut.
import { lazy, Suspense, useState } from "react";
import { Check, KeyRound, Pencil, RefreshCw, RotateCcw, Terminal } from "lucide-react";
import type { ProviderHealth } from "../types";
import { api } from "../api";
import { countdown } from "../format";

// Code-split: xterm.js only loads when the auth console is actually opened,
// keeping the main bundle lean for the phone-first dashboard.
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
  plan: "plan-CLI",
  api: "API",
  open: "open-weight",
  mock: "mock",
  frontier: "frontier",
  agent: "agent",
};

export default function ProvidersPanel({
  providers, now, onRefresh, consoleAvailable, consoleToken, onSetConsoleToken,
}: Props) {
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState("");
  const [authTarget, setAuthTarget] = useState<string | null>(null);

  const handleReset = async (name: string) => {
    await api.resetProviderCooldown(name);
    onRefresh();
  };

  const saveModel = async (instanceId: string) => {
    try {
      await api.setProviderModel(instanceId, modelDraft.trim() || null, consoleToken);
      setEditingModel(null);
      onRefresh();
    } catch {
      /* surfaced via the disabled state / token field */
    }
  };

  const canConsole = consoleAvailable && consoleToken.trim().length > 0;

  // Group accounts under their provider template. A provider with two
  // subscriptions (e.g. claude:work + claude:personal) shows one header and
  // two cards, each with its own health/cooldown.
  const groups: { template: string; instances: ProviderHealth[] }[] = [];
  for (const p of providers) {
    let g = groups.find((x) => x.template === p.template);
    if (!g) {
      g = { template: p.template, instances: [] };
      groups.push(g);
    }
    g.instances.push(p);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted">
          Which plans have capacity right now. Headroom is an{" "}
          <span className="text-defer">estimate</span> — the reliable guarantee is failover on observed limits.
        </p>
        <button onClick={onRefresh} className="flex items-center gap-1 rounded-lg border border-edge px-2 py-1 text-xs text-muted hover:text-ink">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      {/* Console unlock — only when the backend enables it. Token is held in
          React state, never persisted. Unlocks per-account model edit + re-auth. */}
      {consoleAvailable && (
        <div className="flex items-center gap-2 rounded-lg border border-edge bg-panel/60 px-3 py-2">
          <Terminal size={14} className={canConsole ? "text-live" : "text-muted"} />
          <input
            type="password"
            value={consoleToken}
            onChange={(e) => onSetConsoleToken(e.target.value)}
            placeholder="console token (WEB_CONSOLE_TOKEN) to set models / re-auth"
            className="flex-1 rounded border border-edge bg-base px-2 py-1 font-mono text-xs text-ink outline-none focus:border-amber/60"
          />
          <span className={`text-[11px] ${canConsole ? "text-ok" : "text-muted"}`}>{canConsole ? "unlocked" : "locked"}</span>
        </div>
      )}

      {groups.map((group) => (
      <div key={group.template} className="space-y-2">
        {group.instances.length > 1 && (
          <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider text-muted">
            <span className="text-ink">{group.template}</span>
            <span className="rounded bg-base px-1.5 py-0.5 text-[10px]">{group.instances.length} accounts</span>
          </div>
        )}
      <div className="stagger grid grid-cols-1 gap-3 md:grid-cols-2">
        {group.instances.map((p) => {
          const isExtra = p.name !== p.template;
          const cooling = !!p.cooldown_until && new Date(p.cooldown_until).getTime() > now;
          const unhealthy = !p.healthy;  // covers both cooling and offline
          const usedPct = p.est_used_pct != null ? Math.round(p.est_used_pct * 100) : null;
          const barColor = usedPct == null ? "bg-edge" : usedPct > 85 ? "bg-bad" : usedPct > 60 ? "bg-defer" : "bg-amber";

          return (
            <div key={p.name} className="rounded-xl border border-edge bg-panel/70 p-4">
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`h-2.5 w-2.5 rounded-full ${
                        cooling ? "bg-defer" : p.healthy ? "bg-ok" : "bg-bad"
                      }`}
                    />
                    <span className="font-mono text-sm font-semibold text-ink">{p.label || p.name}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    {isExtra && (
                      <span className="rounded bg-base px-1.5 py-0.5 font-mono text-[10px] text-muted">{p.name}</span>
                    )}
                    <span className="rounded bg-base px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                      {TIER_LABEL[p.tier] || p.tier}
                    </span>
                    <span className="rounded bg-base px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                      {p.mode}
                    </span>
                    {(() => {
                      // CLI plans own their model via the CLI's own picker (use the
                      // console); only API providers take a model override here.
                      const modelSettable = p.kind !== "cli";
                      if (p.kind === "cli") {
                        return (
                          <span
                            className="rounded bg-base px-1.5 py-0.5 font-mono text-[10px] text-muted"
                            title="Plan-CLI model is set inside the CLI — open re-auth console and use its /model picker."
                          >
                            {p.model || "CLI default"}
                          </span>
                        );
                      }
                      if (editingModel === p.name) {
                        return (
                          <span className="flex items-center gap-1">
                            <input
                              autoFocus
                              value={modelDraft}
                              onChange={(e) => setModelDraft(e.target.value)}
                              onKeyDown={(e) => e.key === "Enter" && saveModel(p.name)}
                              placeholder={p.model || "model id"}
                              className="w-32 rounded border border-amber/50 bg-base px-1.5 py-0.5 font-mono text-[10px] text-ink outline-none"
                            />
                            <button onClick={() => saveModel(p.name)} className="text-ok hover:text-ink"><Check size={12} /></button>
                          </span>
                        );
                      }
                      return (
                        <span className="flex items-center gap-1 rounded bg-base px-1.5 py-0.5 font-mono text-[10px] text-muted">
                          {p.model || "default model"}
                          {canConsole && modelSettable && (
                            <button
                              title="Set model"
                              onClick={() => { setEditingModel(p.name); setModelDraft(p.model || ""); }}
                              className="text-muted hover:text-amber"
                            >
                              <Pencil size={10} />
                            </button>
                          )}
                        </span>
                      );
                    })()}
                  </div>
                </div>

                <span
                  className={`font-mono text-xs ${
                    cooling ? "text-defer" : p.healthy ? "text-ok" : "text-bad"
                  }`}
                >
                  {cooling ? "cooling" : p.healthy ? "healthy" : "cooling"}
                </span>
              </div>

              {/* Cooldown countdown + manual reset */}
              {(cooling || unhealthy) && (
                <div className="mt-2 flex items-center justify-between">
                  <span className="font-mono text-xs text-defer">
                    {cooling ? `resets in ${countdown(p.cooldown_until, now)}` : "in cooldown"}
                  </span>
                  <button
                    onClick={() => handleReset(p.name)}
                    title="Manually clear this provider's cooldown"
                    className="flex items-center gap-1 rounded-lg border border-defer/40 px-2 py-0.5 font-mono text-[11px] text-defer hover:border-defer hover:text-ink"
                  >
                    <RotateCcw size={11} /> reset
                  </button>
                </div>
              )}

              {/* Headroom estimate */}
              <div className="mt-3">
                <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider text-muted">
                  <span>headroom (est.)</span>
                  <span>{usedPct == null ? "unknown" : `${100 - usedPct}% left`}</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-base">
                  <div
                    className={`h-full ${barColor} transition-all`}
                    style={{ width: `${usedPct == null ? 0 : usedPct}%` }}
                  />
                </div>
              </div>

              {/* Re-auth (plan-CLI logins live on the host volume) */}
              {p.mode === "plan" && (
                <div className="mt-3 border-t border-edge pt-2">
                  {canConsole ? (
                    <button
                      onClick={() => setAuthTarget(p.name)}
                      title="Run this plan's official CLI login here and complete it from the UI."
                      className="flex items-center gap-1.5 font-mono text-[11px] text-amber hover:text-ink"
                    >
                      <Terminal size={12} /> re-auth in console
                    </button>
                  ) : (
                    <button
                      title="Run `make auth` on the host (or unlock the console above) to re-log this plan's official CLI."
                      className="flex items-center gap-1.5 font-mono text-[11px] text-muted hover:text-amber"
                    >
                      <KeyRound size={12} /> re-auth via `make auth p={p.name}`
                    </button>
                  )}
                </div>
              )}
            </div>
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

