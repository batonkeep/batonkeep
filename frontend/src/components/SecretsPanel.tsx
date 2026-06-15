// SecretsPanel.tsx — the named secrets-management surface (P-0009 #3) + key/model
// entry (P-0035). For every key-backed provider it reports where its credential
// resolves from (encrypted store / deployment env / missing), a masked last-4 hint,
// the effective model, and lets the operator paste a BYO key and set the model id.
// Never shows or returns any plaintext key.
// D-track: composed from ui/ primitives (Badge, Button, Card, Input, StatusDot).
import { useCallback, useEffect, useState } from "react";
import { Check, KeyRound, Pencil, RefreshCw, ShieldCheck, Sliders, Star, Trash2, X } from "lucide-react";
import type { ProviderCatalog, SecretStatus } from "../types";
import { api } from "../api";
import { Badge, Button, Card, Input, StatusDot } from "../ui";

function priceLabel(m: { cost_in_per_mtok: number | null; cost_out_per_mtok: number | null }): string {
  if (m.cost_in_per_mtok == null || m.cost_out_per_mtok == null) return "no price";
  return `$${m.cost_in_per_mtok}/$${m.cost_out_per_mtok} per Mtok`;
}

// Compact catalog editor (P-0049): enable/disable models, set the per-capability
// preferred model (the substrate cost-aware routing will later consume), and mark a
// provider default. Pricing edits route through the existing flat overlay seam.
function ModelManager({
  catalog, busy, onChange,
}: {
  catalog: ProviderCatalog;
  busy: boolean;
  onChange: (fn: () => Promise<unknown>) => void;
}) {
  const t = catalog.template;
  const enabled = catalog.models.filter((m) => m.enabled);
  return (
    <div className="space-y-2 rounded-lg border border-edge bg-base/30 p-2">
      <div>
        <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-muted">
          Preferred per capability
        </span>
        <div className="grid grid-cols-2 gap-1 sm:grid-cols-3">
          {catalog.capabilities_vocab.map((cap) => (
            <label key={cap} className="flex flex-col gap-0.5">
              <span className="font-mono text-[9px] text-muted">{cap}</span>
              <select
                disabled={busy}
                value={catalog.preferred[cap] ?? ""}
                onChange={(e) =>
                  onChange(() => api.setCatalogPreferred(t, cap, e.target.value || null))}
                className="rounded border border-edge bg-base px-1 py-0.5 font-mono text-[10px] text-ink"
              >
                <option value="">— inherit default —</option>
                {enabled.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
              </select>
            </label>
          ))}
        </div>
      </div>
      <div>
        <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-muted">
          Models ({enabled.length}/{catalog.models.length} enabled)
        </span>
        <div className="max-h-44 space-y-0.5 overflow-y-auto">
          {catalog.models.map((m) => {
            const isDefault = catalog.preferred.default === m.id;
            return (
              <div key={m.id} className="flex items-center justify-between gap-2 rounded px-1 py-0.5 hover:bg-base/60">
                <div className="flex min-w-0 items-center gap-1.5">
                  <button
                    type="button" disabled={busy}
                    title={isDefault ? "default model" : "set as default"}
                    onClick={() => onChange(() => api.setCatalogPreferred(t, "default", m.id))}
                    className={isDefault ? "text-brand" : "text-muted hover:text-brand"}
                  >
                    <Star size={11} fill={isDefault ? "currentColor" : "none"} />
                  </button>
                  <span className={`truncate font-mono text-[11px] ${m.enabled ? "text-ink" : "text-muted line-through"}`}>
                    {m.id}
                  </span>
                  {!m.known && <Badge tone="neutral">no price</Badge>}
                  {m.use_count > 0 && <span className="font-mono text-[9px] text-muted">{m.use_count}×</span>}
                </div>
                <button
                  type="button" disabled={busy}
                  onClick={() => onChange(() => api.updateCatalogModel(t, { id: m.id, enabled: !m.enabled }))}
                  className="shrink-0 font-mono text-[10px] text-muted hover:text-brand"
                >
                  {m.enabled ? "disable" : "enable"}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

const SOURCE_TONE: Record<SecretStatus["source"], "ok" | "neutral" | "bad"> = {
  stored: "ok",
  env: "neutral",
  missing: "bad",
};

const SOURCE_LABEL: Record<SecretStatus["source"], string> = {
  stored: "stored key",
  env: "deployment env",
  missing: "no key",
};

function lastUsed(ts: string | null): string {
  if (!ts) return "never used";
  const d = new Date(ts);
  return `last used ${d.toLocaleDateString()}`;
}

export default function SecretsPanel() {
  const [rows, setRows] = useState<SecretStatus[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [keyDraft, setKeyDraft] = useState("");
  const [modelDraft, setModelDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [manage, setManage] = useState(false);

  const load = useCallback(() => api.getSecretsStatus().then(setRows).catch(() => { }), []);
  useEffect(() => { load(); }, [load]);

  const loadCatalog = useCallback(
    (provider: string) =>
      api.getProviderCatalog(provider).then(setCatalog).catch(() => setCatalog(null)),
    [],
  );

  const openEditor = (r: SecretStatus) => {
    setEditing(r.provider);
    setKeyDraft("");
    setModelDraft(r.model ?? "");
    setError(null);
    setManage(false);
    setCatalog(null);
    loadCatalog(r.provider);
  };
  const closeEditor = () => {
    setEditing(null); setKeyDraft(""); setModelDraft(""); setError(null);
    setCatalog(null); setManage(false);
  };

  // Save whatever changed: a non-empty key is stored; a model that differs is set.
  const save = async (r: SecretStatus) => {
    setBusy(true);
    setError(null);
    try {
      if (keyDraft.trim()) {
        await api.createCredential(r.provider, keyDraft.trim());
      }
      const nextModel = modelDraft.trim();
      if (nextModel !== (r.model ?? "")) {
        await api.setProviderModel(r.provider, nextModel || null);
      }
      await load();
      closeEditor();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const removeKey = async (r: SecretStatus) => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteCredential(r.provider);
      await load();
      closeEditor();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldCheck size={15} className="text-brand" />
          <span className="font-mono text-sm font-semibold text-ink">provider keys</span>
        </div>
        <Button variant="outline" size="sm" icon={<RefreshCw size={12} />} onClick={load}>
          Refresh
        </Button>
      </div>
      <p className="mt-1 text-xs text-muted">
        Add an API key and pick the model for each provider. BYO keys are encrypted at rest
        and never displayed; <span className="text-brand">local</span> providers need no remote key.
      </p>

      <div className="mt-3 divide-y divide-edge">
        {rows.length === 0 && (
          <p className="py-3 font-mono text-xs text-muted">no key-backed providers</p>
        )}
        {rows.map((r) => {
          const isEditing = editing === r.provider;
          return (
            <div key={r.provider} className="py-2">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <StatusDot tone={r.source === "missing" ? "bad" : "ok"} />
                  <span className="truncate font-mono text-sm text-ink">{r.provider}</span>
                  {r.local && <Badge tone="ok">local</Badge>}
                  {r.model && <Badge tone="neutral">{r.model}</Badge>}
                  {r.key_hint && <span className="font-mono text-[11px] text-muted">{r.key_hint}</span>}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className="hidden font-mono text-[10px] text-muted sm:inline">{lastUsed(r.last_used_at)}</span>
                  <Badge tone={SOURCE_TONE[r.source]}>{SOURCE_LABEL[r.source]}</Badge>
                  <button
                    onClick={() => (isEditing ? closeEditor() : openEditor(r))}
                    className="text-muted hover:text-brand"
                    title={isEditing ? "Close" : "Add key / set model"}
                  >
                    {isEditing ? <X size={13} /> : <Pencil size={13} />}
                  </button>
                </div>
              </div>

              {isEditing && (
                <div className="mt-2 space-y-2 rounded-lg border border-edge bg-base/40 p-3">
                  <label className="block">
                    <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-muted">API key</span>
                    <Input
                      type="password"
                      autoFocus
                      value={keyDraft}
                      onChange={(e) => setKeyDraft(e.target.value)}
                      placeholder={r.source === "stored" ? "paste a new key to rotate" : "paste API key"}
                      className="w-full py-1 font-mono text-xs"
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 flex items-center justify-between font-mono text-[10px] uppercase tracking-wider text-muted">
                      <span>Model id</span>
                      {catalog && (
                        <button
                          type="button"
                          onClick={() => setManage((v) => !v)}
                          className="flex items-center gap-1 normal-case text-muted hover:text-brand"
                          title="Manage models (enable/disable, default, pricing)"
                        >
                          <Sliders size={10} /> {manage ? "Hide" : "Manage models"}
                        </button>
                      )}
                    </span>
                    <Input
                      list={`models-${r.provider}`}
                      value={modelDraft}
                      onChange={(e) => setModelDraft(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && !busy && save(r)}
                      placeholder="pick from the list or type any model id"
                      className="w-full py-1 font-mono text-xs"
                    />
                    <datalist id={`models-${r.provider}`}>
                      {(catalog?.models ?? []).filter((m) => m.enabled).map((m) => (
                        <option key={m.id} value={m.id}>
                          {priceLabel(m)}{m.use_count ? ` · used ${m.use_count}×` : ""}
                        </option>
                      ))}
                    </datalist>
                    {catalog && (() => {
                      const picked = catalog.models.find((m) => m.id === modelDraft.trim());
                      return (
                        <span className="mt-1 block font-mono text-[10px] text-muted">
                          {picked
                            ? `${priceLabel(picked)}${picked.use_count ? ` · used ${picked.use_count}×` : ""}`
                            : modelDraft.trim()
                              ? "unlisted id — will be used as-is"
                              : `default: ${catalog.preferred.default ?? catalog.effective_model ?? "—"}`}
                        </span>
                      );
                    })()}
                  </label>

                  {manage && catalog && (
                    <ModelManager
                      catalog={catalog}
                      busy={busy}
                      onChange={async (fn) => {
                        setBusy(true); setError(null);
                        try { await fn(); await loadCatalog(r.provider); }
                        catch (e) { setError(e instanceof Error ? e.message : "Update failed"); }
                        finally { setBusy(false); }
                      }}
                    />
                  )}
                  {error && <p className="font-mono text-[11px] text-bad">{error}</p>}
                  <div className="flex items-center justify-between">
                    {r.source === "stored" ? (
                      <Button
                        variant="outline" size="sm" icon={<Trash2 size={11} />}
                        onClick={() => removeKey(r)} disabled={busy}
                        className="border-bad/40 text-bad hover:border-bad"
                      >
                        Remove key
                      </Button>
                    ) : <span />}
                    <Button
                      size="sm" icon={<Check size={12} />}
                      onClick={() => save(r)}
                      disabled={busy || (!keyDraft.trim() && modelDraft.trim() === (r.model ?? ""))}
                    >
                      {busy ? "Saving…" : "Save"}
                    </Button>
                  </div>
                  <p className="flex items-center gap-1 font-mono text-[10px] text-muted">
                    <KeyRound size={10} /> stored encrypted; key is never shown again after saving.
                  </p>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
