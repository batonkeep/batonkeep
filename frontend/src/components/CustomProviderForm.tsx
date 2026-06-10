// CustomProviderForm.tsx — D-0026. Inline create/edit form for operator-defined
// local / Ollama / open-API provider endpoints. Rendered inside ProvidersPanel.
//
// Create mode: id, label, endpoint URL, default model, auth type, local toggle.
// Edit mode: same fields pre-filled; id is read-only (it is the stable key).
// On save the parent refreshes both the custom-providers list and the /providers
// health list so the new provider appears in the AI Plans card immediately.

import { useEffect, useId, useState } from "react";
import { Check, Loader2, Server, ShieldCheck, X } from "lucide-react";
import { api } from "../api";
import type { CustomProvider, CustomProviderAuthType } from "../types";
import { Badge, Button, Card } from "../ui";
import { Field, FIELD_BASE } from "../ui/Input";
import TagEditor from "./TagEditor";

// ── Helpers ───────────────────────────────────────────────────────────────────

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface FormState {
  id: string;
  label: string;
  base_url: string;
  default_model: string;
  auth_type: CustomProviderAuthType;
  env_key: string;
  local: boolean;
  extra_models: string;
  capability_tags: string[];
}

function emptyForm(): FormState {
  return {
    id: "",
    label: "",
    base_url: "",
    default_model: "",
    auth_type: "none",
    env_key: "",
    local: false,
    extra_models: "",
    capability_tags: [],
  };
}

function fromExisting(cp: CustomProvider): FormState {
  return {
    id: cp.id,
    label: cp.label,
    base_url: cp.base_url,
    default_model: cp.default_model,
    auth_type: cp.auth_type,
    env_key: cp.env_key ?? "",
    local: cp.local,
    extra_models: cp.extra_models,
    capability_tags: cp.capability_tags ?? [],
  };
}

// ── Auth-type descriptors ─────────────────────────────────────────────────────

const AUTH_OPTIONS: { value: CustomProviderAuthType; label: string; hint: string }[] = [
  { value: "none", label: "None", hint: "Ollama and most local endpoints" },
  { value: "bearer", label: "Bearer token", hint: "Authorization: Bearer <key>" },
  { value: "api_key_header", label: "API key header", hint: "x-api-key: <key>" },
];

// ── Props ─────────────────────────────────────────────────────────────────────

interface Props {
  /** Pass an existing provider to enter edit mode; omit for create. */
  existing?: CustomProvider;
  onSaved: () => void;
  onCancel: () => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function CustomProviderForm({ existing, onSaved, onCancel }: Props) {
  const uid = useId();
  const isEdit = Boolean(existing);

  const [form, setForm] = useState<FormState>(() =>
    existing ? fromExisting(existing) : emptyForm()
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-generate slug from label (create mode only).
  useEffect(() => {
    if (!isEdit && form.label) {
      setForm((prev) => ({ ...prev, id: slugify(prev.label) }));
    }
  }, [form.label, isEdit]);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setError(null);
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      if (isEdit && existing) {
        await api.updateCustomProvider(existing.id, {
          label: form.label,
          base_url: form.base_url,
          default_model: form.default_model,
          auth_type: form.auth_type,
          env_key: form.env_key || null,
          local: form.local,
          extra_models: form.extra_models,
          capability_tags: form.capability_tags,
        });
      } else {
        await api.createCustomProvider({
          id: form.id,
          label: form.label,
          base_url: form.base_url,
          default_model: form.default_model,
          auth_type: form.auth_type,
          env_key: form.env_key || null,
          local: form.local,
          extra_models: form.extra_models,
          capability_tags: form.capability_tags,
        });
      }
      onSaved();
    } catch (err: any) {
      setError(err?.message ?? "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="p-4 border border-brand/20 bg-brand/5">
      <form onSubmit={handleSave} className="space-y-4">
        {/* ── Header ── */}
        <div className="flex items-center gap-2">
          <Server size={14} className="shrink-0 text-brand" />
          <span className="font-mono text-sm font-semibold text-ink">
            {isEdit ? "Edit provider" : "Add custom provider"}
          </span>
          {form.local && (
            <Badge tone="ok" className="ml-auto flex items-center gap-1">
              <ShieldCheck size={11} />
              sovereign
            </Badge>
          )}
        </div>

        {/* ── Label ── */}
        <Field label="Display name" htmlFor={`${uid}-label`}>
          <input
            id={`${uid}-label`}
            className={`${FIELD_BASE} h-9`}
            placeholder="My Ollama"
            value={form.label}
            onChange={(e) => set("label", e.target.value)}
            required
          />
        </Field>

        {/* ── Slug (id) ── */}
        <Field
          label="Provider ID"
          htmlFor={`${uid}-id`}
          hint={
            isEdit
              ? "ID is fixed after creation"
              : "Auto-generated from label — lowercase alphanum + hyphens"
          }
        >
          <input
            id={`${uid}-id`}
            className={`${FIELD_BASE} h-9 font-mono ${isEdit ? "opacity-50" : ""}`}
            placeholder="my-ollama"
            value={form.id}
            onChange={(e) => !isEdit && set("id", e.target.value)}
            readOnly={isEdit}
            required
            pattern="[a-z0-9][a-z0-9\-]{0,62}"
            title="Lowercase alphanumeric + hyphens"
          />
        </Field>

        {/* ── Endpoint URL ── */}
        <Field label="Endpoint URL" htmlFor={`${uid}-url`}>
          <input
            id={`${uid}-url`}
            className={`${FIELD_BASE} h-9 font-mono`}
            placeholder="http://localhost:11434/v1"
            value={form.base_url}
            onChange={(e) => set("base_url", e.target.value)}
            required
            type="url"
          />
        </Field>

        {/* ── Default model ── */}
        <Field label="Default model" htmlFor={`${uid}-model`}>
          <input
            id={`${uid}-model`}
            className={`${FIELD_BASE} h-9 font-mono`}
            placeholder="gemma4:12b"
            value={form.default_model}
            onChange={(e) => set("default_model", e.target.value)}
            required
          />
        </Field>

        {/* ── Capability tags (routing) ── */}
        <div>
          <span className="mb-1.5 block font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
            Capability tags (routing)
          </span>
          <TagEditor
            value={form.capability_tags}
            onChange={(tags) => set("capability_tags", tags)}
          />
          <span className="mt-1 block text-[11px] text-muted">
            Which tasks route here — a task runs on this provider only if its required tags
            overlap these. Leave empty for a sensible default (any · open).
          </span>
        </div>

        {/* ── Auth type ── */}
        <fieldset>
          <legend className="mb-2 block font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
            Authentication
          </legend>
          <div className="space-y-2">
            {AUTH_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className="flex cursor-pointer items-start gap-2.5"
              >
                <input
                  type="radio"
                  name={`${uid}-auth`}
                  value={opt.value}
                  checked={form.auth_type === opt.value}
                  onChange={() => set("auth_type", opt.value)}
                  className="mt-0.5 shrink-0 accent-brand"
                />
                <span className="text-sm">
                  <span className="font-medium text-ink">{opt.label}</span>
                  <span className="ml-1.5 text-[11px] text-muted">{opt.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        {/* ── Env key (shown only when auth != none) ── */}
        {form.auth_type !== "none" && (
          <Field
            label="Env var for API key (optional)"
            htmlFor={`${uid}-envkey`}
            hint="Fallback env var name — the Fernet credential store takes priority"
          >
            <input
              id={`${uid}-envkey`}
              className={`${FIELD_BASE} h-9 font-mono`}
              placeholder="MY_PROVIDER_API_KEY"
              value={form.env_key}
              onChange={(e) => set("env_key", e.target.value)}
            />
          </Field>
        )}

        {/* ── Local / sovereign toggle ── */}
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={form.local}
            onChange={(e) => set("local", e.target.checked)}
            className="mt-0.5 shrink-0 accent-brand"
          />
          <span className="text-sm">
            <span className="flex items-center gap-1.5 font-medium text-ink">
              <ShieldCheck size={13} className="text-teal-400" />
              Keep inference on this machine (sovereign / local)
            </span>
            <span className="mt-0.5 block text-[11px] text-muted">
              Marks this provider as local — confidential sessions will route to it
              instead of any cloud provider (P-0009 sovereignty).
            </span>
          </span>
        </label>

        {/* ── Error ── */}
        {error && (
          <p className="rounded-md bg-red-900/20 px-3 py-2 text-sm text-red-400">
            {error}
          </p>
        )}

        {/* ── Actions ── */}
        <div className="flex items-center justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={saving}>
            <X size={13} />
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="sm" disabled={saving}>
            {saving ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <Check size={13} />
            )}
            {isEdit ? "Save changes" : "Add provider"}
          </Button>
        </div>
      </form>
    </Card>
  );
}
