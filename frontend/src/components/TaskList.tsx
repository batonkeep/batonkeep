// TaskList.tsx — D-0027 items 4 + 6.
// (4) Task cards: left-accent border coloured by last-run status; provider
//     badges on a dedicated teal-tinted row; enable toggle in card footer.
// (6) Empty state: icon + friendly copy + inline New task CTA.
// D-track: composed from ui/ primitives (Button, Badge, Card, StatusDot).
import { useMemo, useState } from "react";
import { CalendarClock, ChevronRight, Pencil, Play, Plus, Search, Sparkles, Trash2 } from "lucide-react";
import type { Run, Task, TaskInput, TaskTemplate } from "../types";
import { STATUS_META, countdown, humanizeSchedule } from "../format";
import { Badge, Button, Card, Input, Select, StatusDot } from "../ui";

interface Props {
  tasks: Task[];
  latestRunByTask: Record<number, Run>;
  now: number;
  busyTaskId: number | null;
  onRun: (task: Task) => void;
  onEdit: (task: Task) => void;
  onDelete: (task: Task) => void;
  onToggle: (task: Task) => void;
  onOpenRun: (runId: number) => void;
  onNewTask?: () => void; // for the empty-state CTA
  templates?: TaskTemplate[]; // starter presets offered when the list is empty
  onUseTemplate?: (input: TaskInput) => void; // pre-fill the form from a preset
}

// Maps a run status to a left-border accent colour.
const ACCENT: Record<string, string> = {
  succeeded:  "border-l-[3px] border-l-ok",
  running:    "border-l-[3px] border-l-live",
  planning:   "border-l-[3px] border-l-live",
  queued:     "border-l-[3px] border-l-live",
  failed:     "border-l-[3px] border-l-bad",
  cancelled:  "border-l-[3px] border-l-bad",
  deferred:   "border-l-[3px] border-l-defer",
};
const accentClass = (status?: string) => (status ? (ACCENT[status] ?? "") : "");

function CandidateBadges({ candidates }: { candidates: string[] }) {
  if (!candidates.length) return <span className="text-muted">—</span>;
  return (
    <span className="flex flex-wrap items-center gap-1">
      {candidates.map((c, i) => (
        <span key={`${c}-${i}`} className="flex items-center gap-1">
          {i > 0 && <ChevronRight size={11} className="text-muted" />}
          {/* teal-tinted provider badge (D-0027 item 4) */}
          <Badge tone="brand">{c}</Badge>
        </span>
      ))}
    </span>
  );
}

export default function TaskList({
  tasks,
  latestRunByTask,
  now,
  busyTaskId,
  onRun,
  onEdit,
  onDelete,
  onToggle,
  onOpenRun,
  onNewTask,
  templates = [],
  onUseTemplate,
}: Props) {
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  // Status options present in the current set, so the filter only offers real values.
  const statuses = useMemo(() => {
    const seen = new Set<string>();
    for (const t of tasks) {
      const s = latestRunByTask[t.id]?.status;
      seen.add(s ?? "never");
    }
    return Array.from(seen);
  }, [tasks, latestRunByTask]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tasks.filter((t) => {
      if (statusFilter !== "all") {
        const s = latestRunByTask[t.id]?.status ?? "never";
        if (s !== statusFilter) return false;
      }
      if (!q) return true;
      return (
        t.name.toLowerCase().includes(q) ||
        (t.description ?? "").toLowerCase().includes(q) ||
        (t.category ?? "").toLowerCase().includes(q)
      );
    });
  }, [tasks, latestRunByTask, query, statusFilter]);

  // ── Empty state (D-0027 item 6) — with one-click starter templates ────────
  if (tasks.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-edge p-10 text-center">
        <CalendarClock size={32} className="mx-auto mb-3 text-muted/50" />
        <p className="mb-1 font-mono text-sm font-semibold text-ink">No tasks yet</p>
        <p className="mb-4 text-xs text-muted">Schedule your first AI task to start orchestrating.</p>
        {onNewTask && (
          <Button variant="primary" size="sm" icon={<Plus size={13} />} onClick={onNewTask}>
            New task
          </Button>
        )}
        {templates.length > 0 && onUseTemplate && (
          <div className="mx-auto mt-7 max-w-md border-t border-edge pt-6 text-left">
            <p className="mb-3 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-muted">
              <Sparkles size={12} /> Start from a template
            </p>
            <div className="flex flex-col gap-2">
              {templates.map((tpl) => (
                <button
                  key={tpl.id}
                  onClick={() => onUseTemplate(tpl.input)}
                  className="rounded-lg border border-edge bg-panel/60 px-4 py-3 text-left transition-colors hover:border-brand/50 hover:bg-brand/5"
                >
                  <span className="block font-mono text-sm font-semibold text-ink">{tpl.label}</span>
                  <span className="mt-0.5 block text-xs text-muted">{tpl.description}</span>
                </button>
              ))}
            </div>
            <p className="mt-3 text-[11px] text-muted">
              Templates open the task form pre-filled and disabled — review the schedule and
              provider, then enable.
            </p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {/* ── Filter toolbar — search + last-run status ── */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tasks…"
            className="pl-9"
            aria-label="Search tasks"
          />
        </div>
        <Select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="h-10 sm:w-44"
          aria-label="Filter by last-run status"
        >
          <option value="all">All statuses</option>
          {statuses.map((s) => (
            <option key={s} value={s}>
              {s === "never" ? "Never run" : STATUS_META[s as keyof typeof STATUS_META]?.label ?? s}
            </option>
          ))}
        </Select>
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-edge p-8 text-center text-xs text-muted">
          No tasks match your filters.
        </div>
      ) : (
        <div className="stagger grid grid-cols-1 gap-3 lg:grid-cols-2">
          {filtered.map((task) => {
        const run = latestRunByTask[task.id];
        const meta = run ? STATUS_META[run.status] : null;
        const candidates = task.routing?.candidates || [];
        const deferred = run?.status === "deferred";
        const healthTone = !meta ? "neutral"
          : meta.dot?.includes("ok") ? "ok"
          : meta.dot?.includes("bad") ? "bad"
          : meta.dot?.includes("live") ? "live"
          : meta.dot?.includes("defer") ? "defer"
          : "neutral";

        return (
          <Card
            key={task.id}
            className={`flex flex-col overflow-hidden transition-colors hover:border-brand/40 ${
              task.enabled ? "" : "opacity-60"
            } ${accentClass(run?.status)}`}
          >
            {/* ── Card body ── */}
            <div className="flex-1 p-4">
              {/* Title row */}
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex items-center gap-2">
                  {meta && (
                    <button onClick={() => run && onOpenRun(run.id)} title={meta.label}>
                      <StatusDot
                        tone={healthTone}
                        pulse={["queued", "planning", "running"].includes(run?.status || "")}
                      />
                    </button>
                  )}
                  <h3 className="truncate font-mono text-sm font-semibold text-ink">{task.name}</h3>
                </div>
                {task.category && <Badge tone="neutral">{task.category}</Badge>}
              </div>

              {task.description && (
                <p className="mt-2 line-clamp-2 text-xs text-muted">{task.description}</p>
              )}

              {/* Meta rows */}
              <div className="mt-3 flex flex-col gap-1.5 text-xs">
                {/* Provider route — dedicated row, teal badges */}
                <div className="flex items-start gap-2">
                  <span className="mt-0.5 w-16 shrink-0 text-muted">route</span>
                  <CandidateBadges candidates={candidates} />
                </div>
                <div className="flex items-center gap-2">
                  <span className="w-16 shrink-0 text-muted">schedule</span>
                  <span className="font-mono text-ink">
                    {humanizeSchedule(task.schedule_kind, task.schedule_expr, task.timezone)}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="w-16 shrink-0 text-muted">last run</span>
                  {meta ? (
                    <span className={`font-mono ${meta.text}`}>
                      {meta.label}
                      {deferred && run?.deferred_until && (
                        <span className="ml-1 text-defer">· resumes {countdown(run.deferred_until, now)}</span>
                      )}
                    </span>
                  ) : (
                    <span className="text-muted">never</span>
                  )}
                </div>
              </div>
            </div>

            {/* ── Card footer — actions + enable toggle (D-0027 item 4) ── */}
            <div className="flex items-center gap-2 border-t border-edge px-4 py-2.5">
              <Button
                variant="primary"
                size="sm"
                icon={<Play size={13} />}
                onClick={() => onRun(task)}
                disabled={busyTaskId === task.id}
              >
                {busyTaskId === task.id ? "Starting…" : "Run now"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="px-2"
                onClick={() => onEdit(task)}
                title="Edit"
                icon={<Pencil size={14} />}
              />
              <Button
                variant="ghost"
                size="sm"
                className="px-2 hover:text-bad"
                onClick={() => onDelete(task)}
                title="Delete"
                icon={<Trash2 size={14} />}
              />
              {/* Enable toggle — footer right, clearly separated from actions */}
              <div className="ml-auto flex items-center gap-2">
                <span className="text-[10px] text-muted">{task.enabled ? "on" : "off"}</span>
                <button
                  onClick={() => onToggle(task)}
                  title={task.enabled ? "Enabled — click to disable" : "Disabled — click to enable"}
                  className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
                    task.enabled ? "bg-brand/80" : "bg-edge"
                  }`}
                >
                  <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-panel transition-all ${
                      task.enabled ? "left-4" : "left-0.5"
                    }`}
                  />
                </button>
              </div>
            </div>
          </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
