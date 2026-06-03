// TaskList.tsx — task cards (§12): name, category, candidate badges (claude › grok › agy),
// human-readable schedule, last-run status dot (incl. deferred countdown), run-now,
// enable toggle, edit/delete.
import { ChevronRight, Pencil, Play, Trash2 } from "lucide-react";
import type { Run, Task } from "../types";
import { STATUS_META, countdown, humanizeSchedule } from "../format";

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
}

function CandidateBadges({ candidates }: { candidates: string[] }) {
  if (!candidates.length) return <span className="text-muted">—</span>;
  return (
    <span className="flex flex-wrap items-center gap-1">
      {candidates.map((c, i) => (
        <span key={`${c}-${i}`} className="flex items-center gap-1">
          {i > 0 && <ChevronRight size={11} className="text-muted" />}
          <span className="rounded border border-edge bg-base px-1.5 py-0.5 font-mono text-[11px] text-ink">
            {c}
          </span>
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
}: Props) {
  if (tasks.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-edge p-8 text-center text-muted">
        No tasks yet. Create one to start orchestrating.
      </div>
    );
  }

  return (
    <div className="stagger grid grid-cols-1 gap-3 lg:grid-cols-2">
      {tasks.map((task) => {
        const run = latestRunByTask[task.id];
        const meta = run ? STATUS_META[run.status] : null;
        const candidates = task.routing?.candidates || [];
        const deferred = run?.status === "deferred";

        return (
          <div
            key={task.id}
            className={`rounded-xl border border-edge bg-panel/70 p-4 transition-colors hover:border-amber/40 ${task.enabled ? "" : "opacity-60"
              }`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  {meta && (
                    <button
                      onClick={() => run && onOpenRun(run.id)}
                      title={meta.label}
                      className="flex items-center gap-1"
                    >
                      <span className={`h-2.5 w-2.5 rounded-full ${meta.dot}`} />
                    </button>
                  )}
                  <h3 className="truncate font-mono text-sm font-semibold text-ink">{task.name}</h3>
                </div>
                {task.category && (
                  <span className="mt-1 inline-block rounded bg-base px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                    {task.category}
                  </span>
                )}
              </div>

              {/* Enable toggle */}
              <button
                onClick={() => onToggle(task)}
                title={task.enabled ? "Enabled — click to disable" : "Disabled — click to enable"}
                className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${task.enabled ? "bg-amber/80" : "bg-edge"
                  }`}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-base transition-all ${task.enabled ? "left-4" : "left-0.5"
                    }`}
                />
              </button>
            </div>

            {task.description && (
              <p className="mt-2 line-clamp-2 text-xs text-muted">{task.description}</p>
            )}

            <div className="mt-3 flex flex-col gap-1.5 text-xs">
              <div className="flex items-center gap-2">
                <span className="w-16 shrink-0 text-muted">route</span>
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

            <div className="mt-3 flex items-center gap-2 border-t border-edge pt-3">
              <button
                onClick={() => onRun(task)}
                disabled={busyTaskId === task.id}
                className="flex items-center gap-1.5 rounded-lg bg-amber/70 px-3 py-1.5 text-xs font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                <Play size={13} />
                {busyTaskId === task.id ? "Starting…" : "Run now"}
              </button>
              <button
                onClick={() => onEdit(task)}
                className="rounded-lg border border-edge p-1.5 text-muted transition-colors hover:text-ink"
                title="Edit"
              >
                <Pencil size={14} />
              </button>
              <button
                onClick={() => onDelete(task)}
                className="rounded-lg border border-edge p-1.5 text-muted transition-colors hover:text-bad"
                title="Delete"
              >
                <Trash2 size={14} />
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
