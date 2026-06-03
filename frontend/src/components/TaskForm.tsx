// TaskForm.tsx — create/edit modal (§12): identity, schedule (+crontab validation),
// output toggles, prompt_template, params key/value editor, and a routing editor with
// a strategy select, drag-to-reorder candidate list, capability tags, failover, overflow.
import { useMemo, useRef, useState } from "react";
import { Check, GripVertical, Plus, Wand2, X } from "lucide-react";
import type { ProviderHealth, RoutingPolicy, RoutingStrategy, Task, TaskInput } from "../types";
import { isValidCron } from "../format";

interface Props {
  task: Task | null; // null → create
  providers: ProviderHealth[];
  onSave: (input: TaskInput, id?: number) => Promise<void>;
  onClose: () => void;
}

interface ParamRow {
  key: string;
  value: string;
}

const STRATEGIES: { id: RoutingStrategy; label: string; hint: string }[] = [
  { id: "capability", label: "capability", hint: "Highest-preference healthy candidate whose tags match" },
  { id: "fixed", label: "fixed", hint: "Always the first candidate" },
  { id: "round_robin", label: "round_robin", hint: "Rotate across healthy candidates to spread quota" },
  { id: "cost_optimized", label: "cost_optimized", hint: "Prefer cheaper tiers / overflow" },
];

const DEFAULT_ROUTING: RoutingPolicy = {
  strategy: "capability",
  candidates: ["mock"],
  capability_tags: [],
  failover: true,
  overflow_to: null,
  max_attempts: 3,
};

// Starter instruction for new tasks — provider-neutral and outcome-focused, so it
// runs the same whether it lands on a single-model API loop or a multi-agent CLI.
const DEFAULT_PROMPT_TEMPLATE = `Research and summarise the latest on {topic}.

Focus on what is most important and recent (prioritise the last {timeframe}); verify claims against reputable primary or secondary sources and link them.

Produce a Markdown report: a \`#\` title, a 2–3 sentence executive summary, then organised sections with inline source links. Be concise, current, and specific — cite every non-obvious claim.`;

// Meta-prompt the user pastes into any LLM chat to author a task instruction.
// Deliberately tells the LLM NOT to prescribe an execution strategy (incl. subagents):
// the same instruction may run on a flat model loop OR a CLI agent and fail over between
// them, so it must stay provider-neutral and outcome-focused.
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

export default function TaskForm({ task, providers, onSave, onClose }: Props) {
  const [name, setName] = useState(task?.name ?? "");
  const [description, setDescription] = useState(task?.description ?? "");
  const [category, setCategory] = useState(task?.category ?? "");
  // New tasks start from a sensible default; editing keeps the saved template (even if empty).
  const [promptTemplate, setPromptTemplate] = useState(task ? task.prompt_template : DEFAULT_PROMPT_TEMPLATE);
  const [scheduleKind, setScheduleKind] = useState<Task["schedule_kind"]>(task?.schedule_kind ?? "none");
  const [scheduleExpr, setScheduleExpr] = useState(task?.schedule_expr ?? "");
  // Default a new task to the browser's timezone so cron times are authored locally
  // (DST-aware on the backend). Editing keeps the task's saved tz.
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [timezone, setTimezone] = useState(task?.timezone ?? browserTz);
  const [wantMarkdown, setWantMarkdown] = useState(task?.want_markdown ?? true);
  const [wantJson, setWantJson] = useState(task?.want_json ?? false);
  const [enabled] = useState(task?.enabled ?? true);
  const [routing, setRouting] = useState<RoutingPolicy>(
    task?.routing ? { ...DEFAULT_ROUTING, ...task.routing } : DEFAULT_ROUTING
  );
  const [params, setParams] = useState<ParamRow[]>(
    task
      ? Object.entries(task.params ?? {}).map(([key, value]) => ({ key, value: String(value) }))
      // New tasks: seed params matching the default template's placeholders.
      : [{ key: "topic", value: "" }, { key: "timeframe", value: "the last 48 hours" }]
  );
  const [tagInput, setTagInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedBuilder, setCopiedBuilder] = useState(false);

  const copyPromptBuilder = async () => {
    const hint = [name, description].filter(Boolean).join(" — ");
    try {
      await navigator.clipboard.writeText(buildMetaPrompt(hint));
      setCopiedBuilder(true);
      setTimeout(() => setCopiedBuilder(false), 1800);
    } catch {
      /* clipboard unavailable */
    }
  };

  const dragIndex = useRef<number | null>(null);

  const providerNames = useMemo(
    () => providers.map((p) => p.name),
    [providers]
  );
  // instance id → human label, for showing accounts (e.g. "Claude (work)") in the
  // candidate editor while routing still stores instance ids.
  const labelFor = useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of providers) m[p.name] = p.label && p.label !== p.name ? p.label : p.name;
    return (id: string) => m[id] ?? id;
  }, [providers]);
  const addableProviders = providerNames.filter((p) => !routing.candidates.includes(p));

  const cronValid = scheduleKind !== "cron" || !scheduleExpr || isValidCron(scheduleExpr);
  const intervalValid =
    scheduleKind !== "interval" || (!!scheduleExpr && !isNaN(parseInt(scheduleExpr, 10)));

  // ── Candidate reorder ────────────────────────────────────────────────────
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
      name: name.trim(),
      description: description || null,
      category: category || null,
      prompt_template: promptTemplate,
      params: paramObj,
      schedule_kind: scheduleKind,
      schedule_expr: scheduleKind === "none" ? null : scheduleExpr,
      timezone: scheduleKind === "cron" ? timezone : "UTC",
      want_markdown: wantMarkdown,
      want_json: wantJson,
      enabled,
      routing,
    };
    setSaving(true);
    try {
      await onSave(input, task?.id);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
      setSaving(false);
    }
  };

  const field = "w-full rounded-lg border border-edge bg-base px-3 py-2 text-sm text-ink outline-none focus:border-amber/60";
  const labelCls = "mb-1 block text-[11px] uppercase tracking-wider text-muted";

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm md:items-center md:p-4">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col rounded-t-2xl border border-edge bg-panel md:rounded-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-edge px-5 py-3">
          <h2 className="font-mono text-sm font-semibold text-ink">
            {task ? "Edit task" : "New task"}
          </h2>
          <button onClick={onClose} className="text-muted hover:text-ink">
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <div>
              <label className={labelCls}>Name</label>
              <input className={field} value={name} onChange={(e) => setName(e.target.value)} placeholder="Daily AI Brief" />
            </div>
            <div>
              <label className={labelCls}>Category</label>
              <input className={field} value={category} onChange={(e) => setCategory(e.target.value)} placeholder="research" />
            </div>
          </div>

          <div>
            <label className={labelCls}>Description</label>
            <input className={field} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>

          {/* Schedule */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <div>
              <label className={labelCls}>Schedule</label>
              <select className={field} value={scheduleKind} onChange={(e) => setScheduleKind(e.target.value as Task["schedule_kind"])}>
                <option value="none">Manual only</option>
                <option value="interval">Interval (seconds)</option>
                <option value="cron">Cron</option>
              </select>
            </div>
            {scheduleKind !== "none" && (
              <div className="md:col-span-2">
                <label className={labelCls}>
                  {scheduleKind === "cron" ? "Crontab (min hour dom mon dow)" : "Interval seconds"}
                </label>
                <input
                  className={`${field} ${(!cronValid || !intervalValid) ? "border-bad" : ""}`}
                  value={scheduleExpr}
                  onChange={(e) => setScheduleExpr(e.target.value)}
                  placeholder={scheduleKind === "cron" ? "0 7 * * *" : "21600"}
                />
              </div>
            )}
            {scheduleKind === "cron" && (
              <div className="md:col-span-2">
                <label className={labelCls}>Timezone · cron is interpreted here (DST-aware)</label>
                <input
                  className={field}
                  list="tz-list"
                  value={timezone}
                  onChange={(e) => setTimezone(e.target.value)}
                  placeholder={browserTz}
                />
                <datalist id="tz-list">
                  <option value={browserTz} />
                  <option value="UTC" />
                  <option value="America/Los_Angeles" />
                  <option value="America/New_York" />
                  <option value="Europe/London" />
                  <option value="Europe/Berlin" />
                  <option value="Asia/Kolkata" />
                  <option value="Asia/Singapore" />
                  <option value="Asia/Tokyo" />
                  <option value="Australia/Sydney" />
                </datalist>
                <p className="mt-1 text-[11px] text-muted">
                  Detected: <span className="text-ink">{browserTz}</span>. Times you enter above fire in this zone, not UTC.
                </p>
              </div>
            )}
          </div>

          {/* Prompt template */}
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <label className={`${labelCls} mb-0`}>Prompt template · use {"{placeholder}"} for params</label>
              <button
                type="button"
                onClick={copyPromptBuilder}
                title="Copy a prompt you can paste into any LLM chat to draft this task's instructions"
                className="flex items-center gap-1 rounded border border-edge px-1.5 py-0.5 text-[11px] text-muted hover:border-amber/50 hover:text-amber"
              >
                {copiedBuilder ? <Check size={11} /> : <Wand2 size={11} />}
                {copiedBuilder ? "Copied" : "AI prompt builder"}
              </button>
            </div>
            <textarea
              className={`${field} min-h-[120px] resize-y font-mono text-xs`}
              value={promptTemplate}
              onChange={(e) => setPromptTemplate(e.target.value)}
            />
            <p className="mt-1 text-[11px] text-muted">
              Describe <span className="text-ink">what to produce</span>, not how to run it — the same prompt may
              run on a model API or a CLI agent and fail over between them. Output structure (Markdown report) is
              enforced automatically.
            </p>
          </div>

          {/* Params */}
          <div>
            <label className={labelCls}>Params</label>
            <div className="space-y-2">
              {params.map((row, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    className={`${field} font-mono`}
                    placeholder="key"
                    value={row.key}
                    onChange={(e) => setParams(params.map((r, idx) => (idx === i ? { ...r, key: e.target.value } : r)))}
                  />
                  <input
                    className={`${field} font-mono`}
                    placeholder="value"
                    value={row.value}
                    onChange={(e) => setParams(params.map((r, idx) => (idx === i ? { ...r, value: e.target.value } : r)))}
                  />
                  <button onClick={() => setParams(params.filter((_, idx) => idx !== i))} className="text-muted hover:text-bad">
                    <X size={16} />
                  </button>
                </div>
              ))}
              <button
                onClick={() => setParams([...params, { key: "", value: "" }])}
                className="flex items-center gap-1 text-xs text-amber hover:opacity-80"
              >
                <Plus size={13} /> Add param
              </button>
            </div>
          </div>

          {/* Output toggles */}
          <div className="flex gap-4">
            <label className="flex items-center gap-2 text-sm text-ink">
              <input type="checkbox" checked={wantMarkdown} onChange={(e) => setWantMarkdown(e.target.checked)} className="accent-amber" />
              Markdown report
            </label>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input type="checkbox" checked={wantJson} onChange={(e) => setWantJson(e.target.checked)} className="accent-amber" />
              JSON output
            </label>
          </div>

          {/* ── Routing editor ──────────────────────────────────────────── */}
          <div className="rounded-xl border border-edge bg-base/50 p-3">
            <div className="mb-3 flex items-center gap-2">
              <span className="font-mono text-xs font-semibold text-amber">routing</span>
              <span className="text-[11px] text-muted">cross-provider order + failover</span>
            </div>

            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <label className={labelCls}>Strategy</label>
                <select
                  className={field}
                  value={routing.strategy}
                  onChange={(e) => setRouting({ ...routing, strategy: e.target.value as RoutingStrategy })}
                >
                  {STRATEGIES.map((s) => (
                    <option key={s.id} value={s.id}>{s.label}</option>
                  ))}
                </select>
                <p className="mt-1 text-[11px] text-muted">{STRATEGIES.find((s) => s.id === routing.strategy)?.hint}</p>
              </div>
              <div>
                <label className={labelCls}>Overflow to (when all plans cooling)</label>
                <select
                  className={field}
                  value={routing.overflow_to ?? ""}
                  onChange={(e) => setRouting({ ...routing, overflow_to: e.target.value || null })}
                >
                  <option value="">none</option>
                  {providerNames.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </div>
            </div>

            {/* Candidate order — drag to reorder */}
            <div className="mt-3">
              <label className={labelCls}>Candidates · drag to reorder (preference order)</label>
              <div className="space-y-1.5">
                {routing.candidates.map((c, i) => (
                  <div
                    key={`${c}-${i}`}
                    draggable
                    onDragStart={() => (dragIndex.current = i)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => {
                      if (dragIndex.current != null) moveCandidate(dragIndex.current, i);
                      dragIndex.current = null;
                    }}
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
                      <button onClick={() => removeCandidate(i)} className="px-1 text-muted hover:text-bad"><X size={14} /></button>
                    </div>
                  </div>
                ))}
              </div>
              {addableProviders.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {addableProviders.map((p) => (
                    <button
                      key={p}
                      onClick={() => addCandidate(p)}
                      className="flex items-center gap-1 rounded border border-edge px-2 py-1 font-mono text-[11px] text-muted hover:border-amber/50 hover:text-ink"
                    >
                      <Plus size={11} /> {labelFor(p)}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Capability tags */}
            <div className="mt-3">
              <label className={labelCls}>Capability tags</label>
              <div className="flex flex-wrap items-center gap-1.5">
                {routing.capability_tags.map((t) => (
                  <span key={t} className="flex items-center gap-1 rounded bg-amber/10 px-2 py-0.5 font-mono text-[11px] text-amber">
                    {t}
                    <button onClick={() => setRouting({ ...routing, capability_tags: routing.capability_tags.filter((x) => x !== t) })}>
                      <X size={11} />
                    </button>
                  </span>
                ))}
                <input
                  className="w-28 rounded border border-edge bg-base px-2 py-1 font-mono text-[11px] text-ink outline-none focus:border-amber/60"
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
                <input type="checkbox" checked={routing.failover} onChange={(e) => setRouting({ ...routing, failover: e.target.checked })} className="accent-amber" />
                Failover
              </label>
              <label className="flex items-center gap-2 text-xs text-muted">
                max attempts
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={routing.max_attempts}
                  onChange={(e) => setRouting({ ...routing, max_attempts: Math.max(1, parseInt(e.target.value || "1", 10)) })}
                  className="w-16 rounded border border-edge bg-base px-2 py-1 font-mono text-ink outline-none focus:border-amber/60"
                />
              </label>
            </div>
          </div>

          {error && <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</div>}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-edge px-5 py-3">
          <button onClick={onClose} className="rounded-lg border border-edge px-4 py-2 text-sm text-muted hover:text-ink">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={saving}
            className="rounded-lg bg-amber/70 px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
          >
            {saving ? "Saving…" : task ? "Save changes" : "Create task"}
          </button>
        </div>
      </div>
    </div>
  );
}
