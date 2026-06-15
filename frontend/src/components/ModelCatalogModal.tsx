// ModelCatalogModal.tsx — P-0049 model catalog editor for one provider, rendered as
// a clean modal off the provider card. Add/enable/disable models, edit per-model
// pricing (write-through to the flat overlay), set the per-capability preferred model
// (the substrate cost-aware routing will later consume) and the provider default.
import { useCallback, useEffect, useState } from "react";
import { Plus, Star } from "lucide-react";
import type { CatalogModel, ProviderCatalog } from "../types";
import { api } from "../api";
import { Badge, Button, Input, Modal, Select } from "../ui";

function priceLabel(m: CatalogModel): string {
  if (m.cost_in_per_mtok == null || m.cost_out_per_mtok == null) return "set price";
  return `$${m.cost_in_per_mtok} / $${m.cost_out_per_mtok}`;
}

export default function ModelCatalogModal({
  template, open, onClose, onSaved,
}: {
  template: string;
  open: boolean;
  onClose: () => void;
  /** Called after any change so the parent can refresh provider health/pricing. */
  onSaved?: () => void;
}) {
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [pin, setPin] = useState("");
  const [pout, setPout] = useState("");
  const [newId, setNewId] = useState("");
  const [newIn, setNewIn] = useState("");
  const [newOut, setNewOut] = useState("");

  const load = useCallback(() => {
    api.getProviderCatalog(template).then(setCatalog).catch(() => setCatalog(null));
  }, [template]);

  useEffect(() => { if (open) { setError(null); load(); } }, [open, load]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); load(); onSaved?.(); }
    catch (e) { setError(e instanceof Error ? e.message : "Update failed"); }
    finally { setBusy(false); }
  };

  const beginEdit = (m: CatalogModel) => {
    setEditId(m.id);
    setPin(m.cost_in_per_mtok != null ? String(m.cost_in_per_mtok) : "");
    setPout(m.cost_out_per_mtok != null ? String(m.cost_out_per_mtok) : "");
  };
  const savePrice = (id: string) => {
    const ci = parseFloat(pin), co = parseFloat(pout);
    const body = Number.isFinite(ci) && Number.isFinite(co)
      ? { id, cost_in_per_mtok: ci, cost_out_per_mtok: co }
      : { id, clear_pricing: true };
    run(() => api.updateCatalogModel(template, body));
    setEditId(null);
  };
  const addModel = () => {
    const id = newId.trim();
    if (!id) return;
    const ci = parseFloat(newIn), co = parseFloat(newOut);
    const pricing = Number.isFinite(ci) && Number.isFinite(co)
      ? { cost_in_per_mtok: ci, cost_out_per_mtok: co } : {};
    run(() => api.updateCatalogModel(template, { id, enabled: true, ...pricing }));
    setNewId(""); setNewIn(""); setNewOut("");
  };
  // Selecting a model sets the catalog default AND clears any stale per-instance
  // runtime override, so the catalog default actually becomes the *effective* model
  // (otherwise an old override would win and the star would diverge from what runs).
  const selectModel = (id: string) => run(async () => {
    await api.setCatalogPreferred(template, "default", id);
    await api.setProviderModel(template, null);
  });

  const enabled = catalog?.models.filter((m) => m.enabled) ?? [];

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="max-w-2xl"
      title={`Models · ${template}`}
      footer={<Button variant="outline" size="sm" onClick={onClose}>Done</Button>}
    >
      {!catalog ? (
        <p className="py-6 text-center font-mono text-xs text-muted">loading catalog…</p>
      ) : (
        <div className="space-y-5">
          {error && <p className="font-mono text-xs text-bad">{error}</p>}

          <p className="font-mono text-[11px] text-muted">
            Active model: <span className="text-ink">{catalog.effective_model ?? "—"}</span>
            <span className="text-muted"> — ★ a model below to switch.</span>
          </p>

          {/* Add a model */}
          <section>
            <h3 className="mb-1.5 font-mono text-[11px] uppercase tracking-wider text-muted">Add a model</h3>
            <div className="flex flex-wrap items-end gap-2">
              <label className="flex min-w-[14rem] flex-1 flex-col gap-1">
                <span className="font-mono text-[10px] text-muted">model id</span>
                <Input
                  value={newId}
                  onChange={(e) => setNewId(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && !busy && addModel()}
                  placeholder="e.g. claude-opus-4-8, gpt-5.4-mini"
                  className="w-full py-1 font-mono text-xs"
                />
              </label>
              <label className="flex w-24 flex-col gap-1">
                <span className="font-mono text-[10px] text-muted">$ in /Mtok</span>
                <Input value={newIn} onChange={(e) => setNewIn(e.target.value)}
                  inputMode="decimal" placeholder="opt." className="w-full py-1 font-mono text-xs" />
              </label>
              <label className="flex w-24 flex-col gap-1">
                <span className="font-mono text-[10px] text-muted">$ out /Mtok</span>
                <Input value={newOut} onChange={(e) => setNewOut(e.target.value)}
                  inputMode="decimal" placeholder="opt." className="w-full py-1 font-mono text-xs" />
              </label>
              <Button size="sm" icon={<Plus size={13} />} disabled={busy || !newId.trim()} onClick={addModel}>
                Add
              </Button>
            </div>
            <p className="mt-1 font-mono text-[10px] text-muted">
              Pricing is optional — known models resolve from the price book. Leave blank to inherit.
            </p>
          </section>

          {/* Model list */}
          <section>
            <h3 className="mb-1.5 font-mono text-[11px] uppercase tracking-wider text-muted">
              Models ({enabled.length}/{catalog.models.length} enabled)
            </h3>
            <div className="divide-y divide-edge rounded-lg border border-edge">
              {catalog.models.length === 0 && (
                <p className="px-3 py-3 font-mono text-xs text-muted">no models — add one above</p>
              )}
              {catalog.models.map((m) => {
                // Star marks the *active* (effective) model, not just the stored
                // preferred — so it always matches what the provider card shows / runs.
                const isActive = catalog.effective_model === m.id;
                const isEditing = editId === m.id;
                return (
                  <div key={m.id} className="px-3 py-2">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex min-w-0 items-center gap-2">
                        <button
                          type="button" disabled={busy}
                          title={isActive ? "active model" : "use this model"}
                          onClick={() => selectModel(m.id)}
                          className={isActive ? "text-brand" : "text-muted hover:text-brand"}
                        >
                          <Star size={13} fill={isActive ? "currentColor" : "none"} />
                        </button>
                        <span className={`truncate font-mono text-xs ${m.enabled ? "text-ink" : "text-muted line-through"}`}>
                          {m.id}
                        </span>
                        {!m.known && <Badge tone="neutral">no price</Badge>}
                        {m.use_count > 0 && (
                          <span className="font-mono text-[10px] text-muted">used {m.use_count}×</span>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-3">
                        <button
                          type="button" disabled={busy}
                          onClick={() => (isEditing ? setEditId(null) : beginEdit(m))}
                          className="font-mono text-[11px] text-muted hover:text-brand"
                          title="Edit $/Mtok pricing"
                        >
                          {priceLabel(m)}
                        </button>
                        <button
                          type="button" disabled={busy}
                          onClick={() => run(() => api.updateCatalogModel(template, { id: m.id, enabled: !m.enabled }))}
                          className="w-14 text-right font-mono text-[11px] text-muted hover:text-brand"
                        >
                          {m.enabled ? "disable" : "enable"}
                        </button>
                      </div>
                    </div>
                    {isEditing && (
                      <div className="mt-2 flex items-center gap-2 pl-6">
                        <Input value={pin} onChange={(e) => setPin(e.target.value)} inputMode="decimal"
                          placeholder="$ in" className="w-20 py-0.5 font-mono text-[11px]" />
                        <Input value={pout} onChange={(e) => setPout(e.target.value)} inputMode="decimal"
                          placeholder="$ out" className="w-20 py-0.5 font-mono text-[11px]" />
                        <span className="font-mono text-[10px] text-muted">/Mtok</span>
                        <Button size="sm" disabled={busy} onClick={() => savePrice(m.id)}>save</Button>
                        <span className="font-mono text-[10px] text-muted">(blank clears)</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>

          {/* Per-capability routing preferences (not the active model — that's the ★
              above, which owns `default`). These are inert until automated cost-aware
              routing (P-0048 lever 4) ships; they pick a model *per task type* then. */}
          <section>
            <h3 className="mb-1.5 font-mono text-[11px] uppercase tracking-wider text-muted">
              Routing preferences <span className="normal-case text-muted/70">· not yet active</span>
            </h3>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {catalog.capabilities_vocab.filter((cap) => cap !== "default").map((cap) => (
                <label key={cap} className="flex flex-col gap-1">
                  <span className="font-mono text-[10px] text-muted">{cap}</span>
                  <Select
                    disabled={busy}
                    value={catalog.preferred[cap] ?? ""}
                    onChange={(e) => run(() => api.setCatalogPreferred(template, cap, e.target.value || null))}
                    className="h-7 text-[11px]"
                  >
                    <option value="">— use active model —</option>
                    {enabled.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
                  </Select>
                </label>
              ))}
            </div>
            <p className="mt-1 font-mono text-[10px] text-muted">
              The ★ above sets the model that runs now. These per-task-type hints are the
              substrate for future cost-aware routing.
            </p>
          </section>
        </div>
      )}
    </Modal>
  );
}
