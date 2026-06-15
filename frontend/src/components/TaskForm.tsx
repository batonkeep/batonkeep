// TaskForm.tsx — create/edit modal (§12): identity, schedule (+crontab validation),
// output toggles, prompt_template, params key/value editor, and a routing editor with
// a strategy select, drag-to-reorder candidate list, capability tags, failover, overflow.
// D-0027 item 3: progressive disclosure — Basic mode (Name, Description, Schedule) +
// collapsible Advanced section (Params, Output toggles, Routing). AI prompt builder
// elevated to a primary teal CTA above the prompt field.
// D-track: composed from ui/ primitives (Modal, Field, Input, Select, Button, Badge, Card).
import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, GripVertical, Plus, Wand2, X } from "lucide-react";
import type { ImageModel, ProviderHealth, RoutingPolicy, RoutingStrategy, Task, TaskInput } from "../types";
import { api } from "../api";
import { isValidCron } from "../format";
import { Badge, Button, Card, Field, Input, Modal, Select } from "../ui";
import CronPicker from "./CronPicker";


interface Props {
  task: Task | null;
  // Optional preset to pre-fill a NEW task (e.g. from a starter template). Ignored
  // when editing an existing `task`; does not change create-vs-edit semantics.
  initial?: TaskInput | null;
  providers: ProviderHealth[];
  onSave: (input: TaskInput, id?: number) => Promise<void>;
  onClose: () => void;
}

interface ParamRow { key: string; value: string; }

const STRATEGIES: { id: RoutingStrategy; label: string; hint: string }[] = [
  { id: "capability",    label: "capability",    hint: "Highest-preference healthy candidate whose tags match" },
  { id: "fixed",         label: "fixed",         hint: "Always the first candidate" },
  { id: "round_robin",   label: "round_robin",   hint: "Rotate across healthy candidates to spread quota" },
  { id: "cost_optimized",label: "cost_optimized",hint: "Prefer cheaper tiers / overflow" },
];

const DEFAULT_ROUTING: RoutingPolicy = {
  strategy: "capability", candidates: ["mock"], capability_tags: [],
  failover: true, overflow_to: null, max_attempts: 3,
};

const DEFAULT_PROMPT_TEMPLATE = `Research and summarise the latest on {topic}.

Focus on what is most important and recent (prioritise the last {timeframe}); verify claims against reputable primary or secondary sources and link them.

Produce a Markdown report: a \`#\` title, a 2–3 sentence executive summary, then organised sections with inline source links. Be concise, current, and specific — cite every non-obvious claim.`;

function buildMetaPrompt(hint: string): string {
  return `I'm configuring an automated task in "batonkeep", a cross-provider research/agent orchestrator. Write the task's instruction prompt for me.

How the instruction will run:
- Unattended, on a schedule; it must produce a polished Markdown report (optionally with a trailing \`\`\`json block of structured data).
- It may be dispatched to ANY of several providers (Claude/Grok/Gemini CLIs, or OpenAI/Anthropic/xAI/Gemini APIs, or an open-weight model) and may FAIL OVER between them. So write provider-neutral, OUTCOME-focused instructions: describe WHAT to cover and WHAT to produce, not HOW to execute. Do NOT prescribe an execution strategy (e.g. "spawn subagents", "use N steps", "think step by step") — let whichever agent runs it decide how.
- The agent can browse the live web and has tools: web_search, web_fetch, flights (fare lookups), file_write.
- Use {placeholder} tokens for anything configurable (e.g. {topic}, {timeframe}, {region}); I'll fill those in separately.

The task I want:
${hint || "<describe your task in a sentence or two>"}

Output ONLY the final instruction prompt, ready to paste — no preamble or commentary.`;
}

const ALL_TIMEZONES: string[] = (() => {
  try { return ["UTC", ...(Intl as any).supportedValuesOf("timeZone") as string[]]; }
  catch { return ["UTC","America/Los_Angeles","America/New_York","Europe/London","Europe/Berlin","Asia/Kolkata","Asia/Singapore","Asia/Tokyo","Australia/Sydney"]; }
})();

export default function TaskForm({ task, initial, providers, onSave, onClose }: Props) {
  // Field defaults come from the task being edited, else a starter preset, else blank.
  // `task` (not `src`) still governs create-vs-edit semantics (button label, save id).
  const src = task ?? initial ?? null;
  const [name, setName] = useState(src?.name ?? "");
  const [description, setDescription] = useState(src?.description ?? "");
  const [category, setCategory] = useState(src?.category ?? "");
  const [promptTemplate, setPromptTemplate] = useState(src ? src.prompt_template : DEFAULT_PROMPT_TEMPLATE);
  const [scheduleKind, setScheduleKind] = useState<Task["schedule_kind"]>(src?.schedule_kind ?? "none");
  const [scheduleExpr, setScheduleExpr] = useState(src?.schedule_expr ?? "");
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [timezone, setTimezone] = useState(src?.timezone ?? browserTz);
  const [wantMarkdown, setWantMarkdown] = useState(src?.want_markdown ?? true);
  const [wantJson, setWantJson] = useState(src?.want_json ?? false);
  const [enabled] = useState(src?.enabled ?? true);
  const [routing, setRouting] = useState<RoutingPolicy>(
    src?.routing ? { ...DEFAULT_ROUTING, ...src.routing } : DEFAULT_ROUTING
  );
  const [params, setParams] = useState<ParamRow[]>(
    src
      ? Object.entries(src.params ?? {}).map(([key, value]) => ({ key, value: String(value) }))
      : [{ key: "topic", value: "" }, { key: "timeframe", value: "the last 48 hours" }]
  );
  const [tagInput, setTagInput] = useState("");
  const [imageModelId, setImageModelId] = useState<string>(src?.image_model_id ?? "");
  const [imageModels, setImageModels] = useState<ImageModel[]>([]); // P-0046 slice 6 catalog
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedBuilder, setCopiedBuilder] = useState(false);
  // Progressive disclosure: Advanced section collapsed by default for new tasks.
  // When editing an existing task that has non-default advanced settings, start open.
  const hasAdvanced = !!src && (
    Object.keys(src.params ?? {}).length > 0 ||
    src.want_json ||
    (src.routing && JSON.stringify(src.routing) !== JSON.stringify(DEFAULT_ROUTING))
  );
  const [showAdvanced, setShowAdvanced] = useState(hasAdvanced);

  useEffect(() => {
    api.listImageModels().then(setImageModels).catch(() => { });
  }, []);

  const copyPromptBuilder = async () => {
    const hint = [name, description].filter(Boolean).join(" — ");
    try {
      await navigator.clipboard.writeText(buildMetaPrompt(hint));
      setCopiedBuilder(true);
      setTimeout(() => setCopiedBuilder(false), 1800);
    } catch { /* clipboard unavailable */ }
  };

  const dragIndex = useRef<number | null>(null);
  // Suspended providers (operator enabled=false) are skipped in routing, so they
  // must not be offered as candidates / overflow targets. Keep them out of the
  // selectable list (existing routing.candidates still render from config).
  const providerNames = useMemo(
    () => providers.filter((p) => p.enabled !== false).map((p) => p.name),
    [providers]
  );
  const labelFor = useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of providers) m[p.name] = p.label && p.label !== p.name ? p.label : p.name;
    return (id: string) => m[id] ?? id;
  }, [providers]);
  const addableProviders = providerNames.filter((p) => !routing.candidates.includes(p));

  const cronValid = scheduleKind !== "cron" || !scheduleExpr || isValidCron(scheduleExpr);
  const intervalValid = scheduleKind !== "interval" || (!!scheduleExpr && !isNaN(parseInt(scheduleExpr, 10)));

  const moveCandidate = (from: number, to: number) => {
    if (to < 0 || to >= routing.candidates.length) return;
    const next = [...routing.candidates];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    setRouting({ ...routing, candidates: next });
  };
  const removeCandidate = (i: number) =>
    setRouting({ ...routing, candidates: routing.candidates.filter((_, idx) => idx !== i) });
  const addCandidate = (name: string) =>
    setRouting({ ...routing, candidates: [...routing.candidates, name] });
  const addTag = () => {
    const t = tagInput.trim();
    if (t && !routing.capability_tags.includes(t)) {
      setRouting({ ...routing, capability_tags: [...routing.capability_tags, t] });
    }
    setTagInput("");
  };

  const submit = async () => {
    setError(null);
    if (!name.trim()) return setError("Name is required.");
    if (!cronValid) return setError("Invalid crontab expression (need 5 fields).");
    if (!intervalValid) return setError("Interval must be a number of seconds.");
    if (routing.candidates.length === 0) return setError("Add at least one routing candidate.");
    const paramObj: Record<string, string> = {};
    for (const { key, value } of params) if (key.trim()) paramObj[key.trim()] = value;
    const input: TaskInput = {
      name: name.trim(), description: description || null, category: category || null,
      prompt_template: promptTemplate, params: paramObj,
      schedule_kind: scheduleKind, schedule_expr: scheduleKind === "none" ? null : scheduleExpr,
      timezone: scheduleKind === "cron" ? timezone : "UTC",
      want_markdown: wantMarkdown, want_json: wantJson, enabled, routing,
      image_model_id: imageModelId || null,
    };
    setSaving(true);
    try { await onSave(input, task?.id); onClose(); }
    catch (e) { setError(e instanceof Error ? e.message : "Save failed."); setSaving(false); }
  };

  return (
    <Modal
      open
      onClose={onClose}
      size="max-w-2xl"
      title={task ? "Edit task" : "New task"}
      footer={
        <>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={saving}>
            {saving ? "Saving…" : task ? "Save changes" : "Create task"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Field label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Daily AI Brief" />
          </Field>
          <Field label="Category">
            <Input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="research" />
          </Field>
        </div>

        <Field label="Description">
          <Input value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>

        {/* Schedule */}
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Field label="Schedule">
              <Select value={scheduleKind} onChange={(e) => setScheduleKind(e.target.value as Task["schedule_kind"])}>
                <option value="none">Manual only</option>
                <option value="interval">Interval (seconds)</option>
                <option value="cron">On a schedule</option>
              </Select>
            </Field>
            {scheduleKind === "interval" && (
              <div className="md:col-span-2">
                <Field label="Interval seconds">
                  <Input
                    value={scheduleExpr}
                    onChange={(e) => setScheduleExpr(e.target.value)}
                    placeholder="21600"
                    className={!intervalValid ? "border-bad" : ""}
                  />
                </Field>
              </div>
            )}
          </div>

          {/* Cron picker — shown when kind = cron */}
          {scheduleKind === "cron" && (
            <div className="rounded-lg border border-edge bg-panel/60 p-3">
              <CronPicker
                value={scheduleExpr}
                onChange={(cron) => setScheduleExpr(cron)}
                hasError={!cronValid}
              />
              {/* Timezone always shown for cron */}
              <div className="mt-3">
                <Field
                  label="Timezone · cron fires in this zone (DST-aware)"
                  hint={`Detected: ${browserTz}. Times above fire in this zone, not UTC.`}
                >
                  <Select value={timezone} onChange={(e) => setTimezone(e.target.value)}>
                    {browserTz !== "UTC" && (
                      <optgroup label="Detected">
                        <option value={browserTz}>{browserTz}</option>
                      </optgroup>
                    )}
                    <optgroup label="All timezones">
                      {ALL_TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
                    </optgroup>
                  </Select>
                </Field>
              </div>
            </div>
          )}
        </div>

        {/* Prompt template + AI prompt builder as primary CTA (D-0027 item 3) */}
        <div>
          {/* Clipboard helper — copies a meta-prompt to paste into your own AI chat.
              NOT in-platform AI; the label must make that clear (D-0027 fix 5). */}
          <Button
            variant="primary"
            size="sm"
            icon={copiedBuilder ? <Check size={13} /> : <Wand2 size={13} />}
            onClick={copyPromptBuilder}
            className="mb-3 w-full justify-center"
            title="Copies a meta-prompt to your clipboard. Paste it into Claude, ChatGPT, or any AI chat to get a ready-made task prompt back."
          >
            {copiedBuilder
              ? "Copied — paste into your AI chat"
              : "Copy prompt template to clipboard"}
          </Button>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
              Prompt template · use {"{placeholder}"} for params
            </span>
          </div>
          <textarea
            className="w-full rounded-md border border-edge bg-base/60 px-3 py-2 font-mono text-xs text-ink outline-none transition-colors focus-visible:border-brand/60 focus-visible:ring-2 focus-visible:ring-brand/30 min-h-[120px] resize-y"
            value={promptTemplate}
            onChange={(e) => setPromptTemplate(e.target.value)}
          />
          <p className="mt-1 text-[11px] text-muted">
            Describe <span className="text-ink">what to produce</span>, not how to run it — the same prompt may
            run on a model API or a CLI agent and fail over between them.
          </p>
        </div>

        {/* Advanced section — collapsible (D-0027 item 3 progressive disclosure) */}
        <div className="rounded-lg border border-edge">
          {/* Toggle header — full-width tap target for mobile */}
          <button
            type="button"
            onClick={() => setShowAdvanced((s) => !s)}
            className="flex w-full items-center justify-between px-4 py-3 text-left"
          >
            <span className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider text-muted">
              {showAdvanced ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              Advanced — params · output · routing
            </span>
            <span className="text-[11px] text-brand">{showAdvanced ? "hide" : "show"}</span>
          </button>

          {showAdvanced && (
            <div className="space-y-4 border-t border-edge px-4 pb-4 pt-3">
              {/* Params */}
              <div>
                <span className="mb-1.5 block font-mono text-[11px] uppercase tracking-wider text-muted">Params</span>
                <div className="space-y-2">
                  {params.map((row, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <Input className="font-mono" placeholder="key" value={row.key}
                        onChange={(e) => setParams(params.map((r, idx) => idx === i ? { ...r, key: e.target.value } : r))} />
                      <Input className="font-mono" placeholder="value" value={row.value}
                        onChange={(e) => setParams(params.map((r, idx) => idx === i ? { ...r, value: e.target.value } : r))} />
                      <Button variant="ghost" size="sm" className="px-1.5 hover:text-bad"
                        icon={<X size={16} />} onClick={() => setParams(params.filter((_, idx) => idx !== i))} />
                    </div>
                  ))}
                  <Button variant="ghost" size="sm" icon={<Plus size={13} />}
                    onClick={() => setParams([...params, { key: "", value: "" }])}
                    className="text-brand hover:opacity-80">
                    Add param
                  </Button>
                </div>
              </div>

              {/* Output toggles */}
              <div className="flex gap-4">
                <label className="flex items-center gap-2 text-sm text-ink">
                  <input type="checkbox" checked={wantMarkdown} onChange={(e) => setWantMarkdown(e.target.checked)} className="brand-brand" />
                  Markdown report
                </label>
                <label className="flex items-center gap-2 text-sm text-ink">
                  <input type="checkbox" checked={wantJson} onChange={(e) => setWantJson(e.target.checked)} className="brand-brand" />
                  JSON output
                </label>
              </div>

              {/* Image-gen model (P-0046 slice 6) */}
              {imageModels.length > 0 && (
                <Field label="Image model" hint="Which model generates images for this task. Default follows the routed provider; you can pick any connected model, including cross-provider.">
                  <Select value={imageModelId} onChange={(e) => setImageModelId(e.target.value)}>
                    <option value="">Default (provider's image model)</option>
                    {imageModels.map((m) => (
                      <option key={m.id} value={m.id} disabled={!m.available}>
                        {`${m.label}${m.available ? "" : " (no key)"}`}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}

              {/* Routing editor */}
              <Card className="p-3">
                <div className="mb-3 flex items-center gap-2">
                  <span className="font-mono text-xs font-semibold text-brand">routing</span>
                  <span className="text-[11px] text-muted">cross-provider order + failover</span>
                </div>

                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <Field label="Strategy"
                    hint={STRATEGIES.find((s) => s.id === routing.strategy)?.hint}>
                    <Select value={routing.strategy}
                      onChange={(e) => setRouting({ ...routing, strategy: e.target.value as RoutingStrategy })}>
                      {STRATEGIES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                    </Select>
                  </Field>
                  <Field label="Overflow to (when all plans cooling)">
                    <Select value={routing.overflow_to ?? ""}
                      onChange={(e) => setRouting({ ...routing, overflow_to: e.target.value || null })}>
                      <option value="">none</option>
                      {providerNames.map((p) => <option key={p} value={p}>{p}</option>)}
                    </Select>
                  </Field>
                </div>

                {/* Candidate order */}
                <div className="mt-3">
                  <span className="mb-1.5 block font-mono text-[11px] uppercase tracking-wider text-muted">
                    Candidates · drag to reorder (preference order)
                  </span>
                  <div className="space-y-1.5">
                    {routing.candidates.map((c, i) => (
                      <div
                        key={`${c}-${i}`}
                        draggable
                        onDragStart={() => (dragIndex.current = i)}
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={() => { if (dragIndex.current != null) moveCandidate(dragIndex.current, i); dragIndex.current = null; }}
                        className="flex items-center gap-2 rounded-lg border border-edge bg-panel px-2 py-1.5"
                      >
                        <GripVertical size={14} className="cursor-grab text-muted" />
                        <span className="font-mono text-[10px] text-muted">{i + 1}</span>
                        <span className="flex-1 font-mono text-sm text-ink">
                          {labelFor(c)}
                          {labelFor(c) !== c && <span className="ml-1.5 text-[10px] text-muted">{c}</span>}
                        </span>
                        <div className="flex items-center gap-1">
                          <button onClick={() => moveCandidate(i, i - 1)} disabled={i === 0} className="px-1 text-muted hover:text-ink disabled:opacity-30">↑</button>
                          <button onClick={() => moveCandidate(i, i + 1)} disabled={i === routing.candidates.length - 1} className="px-1 text-muted hover:text-ink disabled:opacity-30">↓</button>
                          <Button variant="ghost" size="sm" className="px-1 hover:text-bad" icon={<X size={14} />} onClick={() => removeCandidate(i)} />
                        </div>
                      </div>
                    ))}
                  </div>
                  {addableProviders.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {addableProviders.map((p) => (
                        <button key={p} onClick={() => addCandidate(p)}
                          className="flex items-center gap-1 rounded border border-edge px-2 py-1 font-mono text-[11px] text-muted hover:border-brand/50 hover:text-ink">
                          <Plus size={11} /> {labelFor(p)}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                {/* Capability tags */}
                <div className="mt-3">
                  <span className="mb-1.5 block font-mono text-[11px] uppercase tracking-wider text-muted">Capability tags</span>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {routing.capability_tags.map((t) => (
                      <Badge key={t} tone="brand">
                        {t}
                        <button className="ml-1" onClick={() => setRouting({ ...routing, capability_tags: routing.capability_tags.filter((x) => x !== t) })}>
                          <X size={11} />
                        </button>
                      </Badge>
                    ))}
                    <input
                      className="w-28 rounded border border-edge bg-base px-2 py-1 font-mono text-[11px] text-ink outline-none focus:border-brand/60"
                      placeholder="add tag…"
                      value={tagInput}
                      onChange={(e) => setTagInput(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addTag())}
                    />
                  </div>
                </div>

                {/* Failover + max attempts */}
                <div className="mt-3 flex items-center gap-4">
                  <label className="flex items-center gap-2 text-sm text-ink">
                    <input type="checkbox" checked={routing.failover}
                      onChange={(e) => setRouting({ ...routing, failover: e.target.checked })} className="brand-brand" />
                    Failover
                  </label>
                  <label className="flex items-center gap-2 text-xs text-muted">
                    max attempts
                    <input type="number" min={1} max={10} value={routing.max_attempts}
                      onChange={(e) => setRouting({ ...routing, max_attempts: Math.max(1, parseInt(e.target.value || "1", 10)) })}
                      className="w-16 rounded border border-edge bg-base px-2 py-1 font-mono text-ink outline-none focus:border-brand/60"
                    />
                  </label>
                </div>
              </Card>
            </div>
          )}
        </div>

        {error && (
          <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</div>
        )}
      </div>
    </Modal>
  );
}
