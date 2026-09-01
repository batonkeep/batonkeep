// ActivityFeed.tsx — one timeline across every lane (P-0100 / Gate C).
//
// Per-run logs already existed; the *aggregate* did not. An operator who has to open
// three views and correlate by timestamp cannot answer "what happened while I was away",
// which is the only question unattended work actually raises.
//
// The honesty rule this surface exists to keep: it renders the **work** outcome, never
// the transport status. A feed keyed on `succeeded` would show a run that delivered
// nothing as a success — the exact dishonesty P-0070/#182 measured (legacy success_rate
// 1.0 against build_success_rate 0.5385 on the same window).
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Check, Clock, FileText, Hand } from "lucide-react";
import { api } from "../api";
import type { ActivityItem } from "../types";

/** Outcomes that are a clean delivery. Everything else is either a real problem or a
 *  deliberate non-result, and the two are shown differently on purpose. */
const CLEAN = new Set(["succeeded"]);
/** Outcomes that are *not* failures: nothing broke, there was simply nothing to do.
 *  Grading these as failures is how a feed starts lying in the other direction. */
const BENIGN = new Set(["no_proposals", "cancelled", "deferred", "parked"]);

function tone(outcome: string): { cls: string; icon: typeof Check } {
  if (CLEAN.has(outcome)) return { cls: "text-emerald-500", icon: Check };
  if (BENIGN.has(outcome)) return { cls: "text-muted", icon: Clock };
  return { cls: "text-amber-500", icon: AlertTriangle };
}

/** Say what the outcome means, rather than showing a raw enum. These strings are the
 *  whole value of the surface — "outputs_missing" tells an operator nothing. */
const EXPLAIN: Record<string, string> = {
  succeeded: "delivered",
  outputs_missing: "reported success but produced nothing",
  unbacked: "claimed work with no artifact behind it",
  escaped_full: "wrote outside its workspace",
  escaped_partial: "wrote partly outside its workspace",
  escaped_workspace: "wrote outside its workspace",
  no_proposals: "nothing to propose",
  parked: "waiting on you",
  deferred: "waiting on capacity",
  failed: "failed",
  cancelled: "cancelled",
  running: "in flight",
};

function when(iso: string): string {
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

const LANE: Record<string, string> = { run: "task", turn: "session", planner: "planner" };

export default function ActivityFeed() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    api.listActivity({ limit: 60 })
      .then((rows) => { setItems(rows); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);

  if (loaded && items.length === 0) {
    return (
      <div className="rounded-xl border border-edge bg-panel px-4 py-10 text-center text-sm text-muted">
        Nothing has run yet.
        <span className="mt-1 block text-[11px]">
          Scheduled tasks, build sessions and planner turns all appear here.
        </span>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-edge bg-panel">
      {items.map((it) => {
        const t = tone(it.outcome);
        const Icon = t.icon;
        const disagrees = it.status !== it.outcome;
        return (
          <div
            key={`${it.kind}-${it.id}`}
            className="flex items-start gap-3 border-b border-edge px-4 py-3 last:border-b-0"
          >
            <Icon size={15} className={`mt-0.5 shrink-0 ${t.cls}`} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[13px] font-semibold text-ink">{it.actor}</span>
                <span className="shrink-0 font-mono text-[10px] text-muted">{when(it.at)}</span>
              </div>
              <div className="mt-0.5 font-mono text-[11px] text-muted">
                {LANE[it.kind] ?? it.kind}
                {it.provider ? ` · ${it.provider}` : ""}
                {it.cost_usd > 0 ? ` · $${it.cost_usd.toFixed(4)}` : ""}
              </div>
              <div className={`mt-1 text-[12px] ${t.cls}`}>
                {EXPLAIN[it.outcome] ?? it.outcome}
                {/* When the work outcome and the transport status disagree, show both.
                    That disagreement is the finding, not a rendering problem. */}
                {disagrees && (
                  <span className="ml-1.5 font-mono text-[10px] text-muted">
                    (reported {it.status})
                  </span>
                )}
              </div>
              {it.error && (
                <div className="mt-1 line-clamp-2 text-[11px] text-muted">{it.error}</div>
              )}
              <div className="mt-1.5 flex flex-wrap items-center gap-3">
                {it.artifact && (
                  <span className="inline-flex items-center gap-1 font-mono text-[10px] text-muted">
                    <FileText size={11} /> {it.artifact.split("/").slice(-2).join("/")}
                  </span>
                )}
                {it.awaiting_approval_id != null && (
                  <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-brand">
                    <Hand size={12} /> needs your decision — see the inbox
                  </span>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
