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
import { AlertTriangle, Bot, CheckCircle2, Clock, Hand, Pause } from "lucide-react";
import { api } from "../api";
import type { AgentSummary } from "../types";

function ago(iso: string): number {
  return Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
}

function since(secs: number): string {
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function when(iso: string | null): string {
  if (!iso) return "never run";
  return since(ago(iso));
}

/** How long an agent may go without delivering before that is worth saying out loud.
 *
 * A threshold is a number someone has to defend, which is exactly why P-0113 did not
 * make one the discriminator on its own (option (b)). It is fine *here* because it only
 * changes emphasis: the date is shown either way and nothing is hidden when the guess
 * is wrong. 36h clears a daily agent's normal gap plus a missed run. */
const STALE_SECONDS = 36 * 3600;

/** The answer to "is this agent still doing work?" — stated plainly, and deliberately
 *  not derived from any run's status.
 *
 *  An instance that had executed nothing for ten days rendered as healthy on every
 *  surface here, because each one classified statuses and none of them asked this. */
function delivery(a: AgentSummary): { text: string; stale: boolean } {
  if (!a.last_delivered_at) {
    return {
      text: a.runs_total > 0 ? "has never delivered" : "nothing delivered yet",
      stale: a.runs_total > 0,
    };
  }
  const secs = ago(a.last_delivered_at);
  return { text: `delivered ${since(secs)}`, stale: secs > STALE_SECONDS };
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

          {/* The headline is DELIVERY, not the last run's status. An agent whose
              every run is sitting in a benign-looking status is not working, and
              until P-0113 nothing on this card said so. */}
          {(() => {
            const d = delivery(a);
            return (
              <div
                className={`mt-3 flex items-center gap-1.5 text-[12px] font-medium ${
                  d.stale ? "text-amber-500" : "text-ink"
                }`}
              >
                {d.stale ? <AlertTriangle size={12} /> : <CheckCircle2 size={12} />}
                <span>{d.text}</span>
              </div>
            );
          })()}

          <div className="mt-1 flex items-center gap-1.5 text-[11px]">
            <Clock size={11} className="text-muted" />
            <span className="text-muted">last run {when(a.last_run_at)}</span>
            {a.last_outcome && (
              <span className="text-muted">
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
