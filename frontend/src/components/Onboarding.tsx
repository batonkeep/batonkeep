// Onboarding.tsx — three credential paths (§12 / §4.1): plan-CLI login (canonical),
// BYO-key, and hosted open-weight. Framed as "set up the box once, drive from your phone".
import { useState } from "react";
import { Cloud, KeyRound, Terminal, X } from "lucide-react";
import type { Credential, Mode, ProviderHealth } from "../types";

interface Props {
  mode: Mode | null;
  providers: ProviderHealth[];
  credentials: Credential[];
  onAddCredential: (provider: string, apiKey: string, label?: string | null) => Promise<void>;
  onClose: () => void;
}

export default function Onboarding({ mode, providers, credentials, onAddCredential, onClose }: Props) {
  const [provider, setProvider] = useState("openai-api");
  const [apiKey, setApiKey] = useState("");
  const [label, setLabel] = useState("");
  const [customId, setCustomId] = useState(false); // type a custom instance id (e.g. openai-api:team)
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const planProviders = providers.filter((p) => p.mode === "plan");
  const apiProviders = providers.filter((p) => p.mode === "api" || p.mode === "open");
  const planAllowed = mode?.plan_cli_allowed ?? true;
  const haveCred = (name: string) => credentials.some((c) => c.provider === name);

  // Group plan-CLI accounts under their provider template (multiple subscriptions).
  const planGroups: { template: string; instances: ProviderHealth[] }[] = [];
  for (const p of planProviders) {
    let g = planGroups.find((x) => x.template === p.template);
    if (!g) { g = { template: p.template, instances: [] }; planGroups.push(g); }
    g.instances.push(p);
  }

  const save = async () => {
    if (!apiKey.trim() || !provider.trim()) return;
    setSaving(true);
    setMsg(null);
    try {
      await onAddCredential(provider.trim(), apiKey.trim(), label.trim() || null);
      setApiKey("");
      setMsg(`Stored key for ${provider.trim()}.`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Failed to store key.");
    } finally {
      setSaving(false);
    }
  };

  const card = "rounded-xl border border-edge bg-panel/70 p-4";
  const head = "flex items-center gap-2 font-mono text-sm font-semibold text-ink";

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm md:items-center md:p-4">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col rounded-t-2xl border border-edge bg-base md:rounded-2xl">
        <div className="flex items-center justify-between border-b border-edge px-5 py-3">
          <div>
            <h2 className="font-mono text-sm font-semibold text-ink">Connect your providers</h2>
            <p className="text-[11px] text-muted">
              Mode: <span className="text-amber">{mode?.deployment_mode ?? "…"}</span> · set up the box once, drive from any device.
            </p>
          </div>
          <button onClick={onClose} className="text-muted hover:text-ink"><X size={18} /></button>
        </div>

        <div className="space-y-3 overflow-y-auto p-5">
          {/* 1 — Plan-CLI (canonical) */}
          <div className={card}>
            <div className={head}>
              <Terminal size={16} className="text-amber" /> Plan-CLI · canonical
              {!planAllowed && <span className="rounded bg-bad/15 px-1.5 py-0.5 text-[10px] text-bad">disabled in managed</span>}
            </div>
            <p className="mt-2 text-xs text-muted">
              Log your own subscriptions in <span className="text-ink">once on the host</span> — the official CLIs only,
              never their tokens. Logins persist and self-refresh on the <code>agent_home</code> volume.
            </p>
            <pre className="mt-2 rounded-lg border border-edge bg-panel p-2 text-xs text-ink">make auth   # walks through claude › grok › agy › codex sign-in</pre>
            {planAllowed && planGroups.length > 0 && (
              <div className="mt-3 space-y-2">
                {planGroups.map((g) => (
                  <div key={g.template}>
                    {g.instances.length > 1 && (
                      <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted">{g.template}</div>
                    )}
                    <div className="flex flex-wrap gap-1.5">
                      {g.instances.map((p) => (
                        <span key={p.name} className="flex items-center gap-1 rounded border border-edge px-2 py-0.5 font-mono text-[11px]">
                          <span className={`h-1.5 w-1.5 rounded-full ${p.healthy ? "bg-ok" : "bg-muted"}`} />
                          {p.label || p.name}
                          {p.name !== p.template && <span className="text-muted">{p.name}</span>}
                          <span className="text-muted">{p.healthy ? "logged in" : "not logged in"}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {/* Add another subscription of the same provider (Phase B). */}
            <details className="mt-3 text-xs text-muted">
              <summary className="cursor-pointer text-ink hover:text-amber">+ Add another account of a provider</summary>
              <div className="mt-2 space-y-1.5">
                <p>To run a second subscription of the same provider (e.g. two Claude accounts) for rate-limit spreading:</p>
                <ol className="ml-4 list-decimal space-y-1">
                  <li>Declare the instance in <code>provider-instances.json</code> with its own <code>cli_config_dir</code> under <code>/home/agent</code>, and set <code>PROVIDER_INSTANCES_CONFIG</code>.</li>
                  <li>Log it in (writes into that account's dir):</li>
                </ol>
                <pre className="rounded-lg border border-edge bg-panel p-2 text-ink">make auth p=claude:work</pre>
                <p>It then appears above and can be used as a routing candidate.</p>
              </div>
            </details>
          </div>

          {/* 2 — BYO-key */}
          <div className={card}>
            <div className={head}>
              <KeyRound size={16} className="text-amber" /> Bring your own API key
            </div>
            <p className="mt-2 text-xs text-muted">Stored encrypted at rest (Fernet via APP_SECRET). Co-equal with plan-CLI for routing.</p>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              {customId ? (
                <input
                  type="text"
                  placeholder="instance id (e.g. openai-api:team)"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  className="rounded-lg border border-edge bg-panel px-3 py-2 font-mono text-sm text-ink outline-none focus:border-amber/60"
                />
              ) : (
                <select
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  className="rounded-lg border border-edge bg-panel px-3 py-2 text-sm text-ink outline-none focus:border-amber/60"
                >
                  {(apiProviders.length ? apiProviders.map((p) => p.name) : ["openai-api", "claude-api", "grok-api", "gemini-api"]).map((n) => (
                    <option key={n} value={n}>{n}{haveCred(n) ? " ✓" : ""}</option>
                  ))}
                </select>
              )}
              <input
                type="text"
                placeholder="label (optional)"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className="rounded-lg border border-edge bg-panel px-3 py-2 text-sm text-ink outline-none focus:border-amber/60 sm:w-36"
              />
              <input
                type="password"
                placeholder="sk-…"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="flex-1 rounded-lg border border-edge bg-panel px-3 py-2 font-mono text-sm text-ink outline-none focus:border-amber/60"
              />
              <button
                onClick={save}
                disabled={saving || !apiKey.trim() || !provider.trim()}
                className="rounded-lg bg-amber/70 px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
              >
                {saving ? "Storing…" : "Store"}
              </button>
            </div>
            <label className="mt-2 flex items-center gap-1.5 text-[11px] text-muted">
              <input type="checkbox" checked={customId} onChange={(e) => setCustomId(e.target.checked)} />
              Use a custom instance id — for a second key on one provider (e.g. <code>openai-api:team</code>); declare the instance in <code>PROVIDER_INSTANCES_CONFIG</code> to route to it.
            </label>
            {msg && <div className="mt-2 text-xs text-muted">{msg}</div>}
          </div>

          {/* 3 — Hosted open-weight */}
          <div className={card}>
            <div className={head}>
              <Cloud size={16} className="text-amber" /> Hosted open-weight
            </div>
            <p className="mt-2 text-xs text-muted">
              Credential-free: the deployment runs inference itself (set <code>OPENAI_BASE_URL</code>/<code>OPENAI_API_KEY</code> on the
              host). Nothing to do here — tasks can route to <span className="text-ink">open-default</span> out of the box.
            </p>
          </div>
        </div>

        <div className="flex justify-end border-t border-edge px-5 py-3">
          <button onClick={onClose} className="rounded-lg bg-amber/70 px-4 py-2 text-sm font-semibold text-white hover:opacity-90">
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
