// ApprovalInbox.tsx — "what needs me", across every agent, run and project.
//
// P-0100 / Gate B3. Unattended work that can pause for a human is only useful if the
// human can find what it paused on. Before this the only approval surface was a card
// inside one live session, so an unattended run that parked was visible to nobody.
//
// Why a header panel rather than a nav item: the queue is *cross-cutting* — a parked
// `code_exec` belongs to a run (Tasks) while a `canonical_write` belongs to a project
// (Projects), so filing it under either would misfile half of it. The bottom nav is also
// at five items on mobile, which Sidebar.tsx calls the platform ceiling.
import { useCallback, useEffect, useState } from "react";
import { Check, Inbox, X } from "lucide-react";
import { api } from "../api";
import type { Approval } from "../types";
import Button from "../ui/Button";

/** A pending row an operator can actually act on from here.
 *
 * Session code-exec approvals are excluded deliberately: they are decided through their
 * own session route (a live operator watching a live view), and the backend refuses them
 * on the general endpoint. Showing an action here that the API would reject is worse
 * than not showing the row — so the panel lists what it can decide, and says so. */
function isActionable(a: Approval): boolean {
  if (a.status !== "pending") return false;
  if (a.kind === "canonical_write") return true;
  return a.kind === "code_exec" && a.run_id !== null;
}

function describe(a: Approval): { title: string; where: string; body?: string } {
  if (a.kind === "code_exec") {
    const code = typeof a.payload?.code === "string" ? (a.payload.code as string) : undefined;
    const label = typeof a.payload?.label === "string" ? (a.payload.label as string) : undefined;
    return {
      title: label || "Run code",
      where: a.run_id !== null ? `run #${a.run_id}` : `session ${a.session_id ?? "?"}`,
      body: code,
    };
  }
  if (a.kind === "canonical_write") {
    const rel = typeof a.payload?.rel_path === "string" ? (a.payload.rel_path as string) : undefined;
    return {
      title: rel ? `Write ${rel}` : "Canonical write",
      where: a.project_id ? `project ${a.project_id.slice(0, 8)}` : "project",
      body: typeof a.payload?.diff === "string" ? (a.payload.diff as string) : undefined,
    };
  }
  return { title: a.kind, where: a.producer };
}

function age(iso: string): string {
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

interface Props {
  onCountChange?: (n: number) => void;
}

export default function ApprovalInbox({ onCountChange }: Props) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Approval[]>([]);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.listApprovals({ status: "pending" })
      .then((all) => {
        const actionable = all.filter(isActionable);
        setRows(actionable);
        onCountChange?.(actionable.length);
      })
      .catch(() => { /* transient; the next poll retries */ });
  }, [onCountChange]);

  // Polled rather than pushed. A parked run can appear with no client connected, so the
  // count has to be correct on load, not only for whoever was watching when it happened.
  // A real notification channel is a separate decision (see P-0100 notes on B4).
  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  async function decide(a: Approval, approved: boolean) {
    setBusy(a.id);
    setError(null);
    try {
      await api.decideApproval(a.id, approved);
      await load();
    } catch (e) {
      // This message was written before P-0106 and said "a restart ends a parked run".
      // That stopped being true when checkpointing landed — a parked run now *survives*
      // a restart, which is the whole point of Gate B — so the advice ("re-run the task")
      // was telling the operator to duplicate work the engine had preserved. The
      // remaining honest causes are that the run was cancelled (P-0110) or that its
      // checkpoint could not be replayed (the SDK fence refusing, correctly).
      setError(
        e instanceof Error && /no longer running/i.test(e.message)
          ? "That run is no longer waiting — it was cancelled, or its checkpoint could not be resumed. Check the run for the reason."
          : "Could not record that decision.",
      );
      await load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="relative">
      <Button
        variant={rows.length > 0 ? "primary" : "outline"}
        size="sm"
        onClick={() => setOpen((o) => !o)}
        title={rows.length > 0 ? `${rows.length} awaiting your decision` : "Nothing awaiting you"}
        aria-label="Approvals"
        className="px-2.5"
        icon={<Inbox size={14} />}
      >
        {rows.length > 0 ? String(rows.length) : ""}
      </Button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1.5 max-h-[70vh] w-[min(26rem,calc(100vw-2rem))] overflow-y-auto rounded-xl border border-edge bg-panel shadow-lg">
          <div className="flex items-center justify-between border-b border-edge px-4 py-2.5">
            <span className="font-mono text-sm font-semibold text-ink">Needs you</span>
            <span className="text-[11px] text-muted">{rows.length} pending</span>
          </div>

          {error && (
            <div className="border-b border-edge bg-brand/5 px-4 py-2 text-[11px] text-ink">{error}</div>
          )}

          {rows.length === 0 && (
            <div className="px-4 py-6 text-center text-[12px] text-muted">
              Nothing is waiting on you.
              <span className="mt-1 block text-[11px]">
                Agents that pause for a decision show up here.
              </span>
            </div>
          )}

          {rows.map((a) => {
            const d = describe(a);
            return (
              <div key={a.id} className="border-b border-edge px-4 py-3 last:border-b-0">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-semibold text-ink text-[13px]">{d.title}</span>
                  <span className="shrink-0 font-mono text-[10px] text-muted">{age(a.created_at)}</span>
                </div>
                <div className="mt-0.5 font-mono text-[11px] text-muted">
                  {d.where} · {a.producer}
                  {/* P-0106: a checkpointed run survives a restart, so its decision can
                      wait. One holding an in-process wait cannot — say which, because the
                      operator's choice of "later" depends on it. */}
                  {a.kind === "code_exec" && a.run_id !== null && !a.resumable && (
                    <span className="ml-1.5 text-amber-500" title="This run is waiting in memory — a restart ends it.">
                      · decide before restart
                    </span>
                  )}
                </div>
                {d.body && (
                  <pre className="mt-2 max-h-32 overflow-auto rounded-lg bg-base p-2 font-mono text-[10px] leading-relaxed text-ink">
                    {d.body}
                  </pre>
                )}
                <div className="mt-2.5 flex gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={busy === a.id}
                    onClick={() => decide(a, true)}
                    icon={<Check size={13} />}
                  >
                    Approve
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy === a.id}
                    onClick={() => decide(a, false)}
                    icon={<X size={13} />}
                  >
                    Deny
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
