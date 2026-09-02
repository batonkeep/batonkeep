// AgentsPanel.tsx — the operator's agents (P-0107 shape (b) / Gate C1).
//
// A *grouping* over existing nouns, not a new entity: an agent here is a scheduled task
// shown with the context that makes it legible as an actor — its project, what it runs
// on, what it last did, and what it is blocked on. P-0107 found the durable `Agent` is
// the designed target but that minting it before the agent-to-agent boundary is decided
// would settle an authority model by accident.
//
// The number that matters is `recent_failures`, and it counts **delivery**, not transport
// status: an agent reporting success while producing nothing is not healthy (P-0070).
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Bot, Clock, Hand, Pause } from "lucide-react";
import { api } from "../api";
import type { AgentSummary } from "../types";

function when(iso: string | null): string {
  if (!iso) return "never run";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/** Say what happened, not what the enum is called. */
const OUTCOME: Record<string, string> = {
  succeeded: "delivered",
  outputs_missing: "produced nothing",
  unbacked: "claimed work with no artifact",
  escaped_full: "wrote outside its workspace",
  escaped_partial: "wrote partly outside its workspace",
  parked: "waiting on you",
  deferred: "waiting on capacity",
  failed: "failed",
  running: "running now",
};

export default function AgentsPanel() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    api.listAgents()
      .then((a) => { setAgents(a); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  if (loaded && agents.length === 0) {
    return (
      <div className="rounded-xl border border-edge bg-panel px-4 py-10 text-center text-sm text-muted">
        No agents yet.
        <span className="mt-1 block text-[11px]">
          A task with a schedule becomes an agent — something that persists and acts on its
          own, rather than a job you ran once.
        </span>
      </div>
    );
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {agents.map((a) => (
        <div
          key={a.principal_id}
          className={`rounded-xl border bg-panel p-4 ${
            a.awaiting > 0 ? "border-brand/50" : "border-edge"
          }`}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <Bot size={16} className={a.enabled ? "text-brand" : "text-muted"} />
              <span className="truncate text-[13px] font-semibold text-ink">{a.name}</span>
            </div>
            {!a.enabled && (
              <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-muted">
                <Pause size={11} /> paused
              </span>
            )}
          </div>

          <div className="mt-1 font-mono text-[10px] text-muted">
            {a.project_name ? `${a.project_name} · ` : ""}
            {a.schedule_expr}
            {a.provider ? ` · ${a.provider}` : ""}
          </div>

          <div className="mt-3 flex items-center gap-1.5 text-[12px]">
            <Clock size={12} className="text-muted" />
            <span className="text-muted">{when(a.last_run_at)}</span>
            {a.last_outcome && (
              <span className="text-ink">
                · {OUTCOME[a.last_outcome] ?? a.last_outcome}
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px]">
            <span className="font-mono text-muted">{a.runs_total} runs</span>
            {/* Counts runs that did not *deliver*, so a run reporting success while
                producing nothing is included. That is the whole point of the number. */}
            {a.recent_failures > 0 && (
              <span className="inline-flex items-center gap-1 text-amber-500">
                <AlertTriangle size={12} />
                {a.recent_failures} of its last 20 didn&rsquo;t deliver
              </span>
            )}
            {a.awaiting > 0 && (
              <span className="inline-flex items-center gap-1 font-semibold text-brand">
                <Hand size={12} /> {a.awaiting} waiting on you
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
