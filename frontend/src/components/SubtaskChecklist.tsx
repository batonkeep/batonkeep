// SubtaskChecklist.tsx — P-0069 B2: a WorkItem's sub-task checklist = output
// contract + grounded progress. Verifiable items (with an `expected` path/glob)
// flip to ✓ verified only when the artifact lands in a bound session's committed
// tree; asserted items can be marked done but read as "claimed". Items are
// agent-proposed and operator-confirmed/modified (P-0078 planner will propose).
import { useState } from "react";
import { Check, CheckCircle2, Circle, Plus, Trash2 } from "lucide-react";
import type { SubtaskItem, SubtaskItemInput, WorkItem } from "../types";
import { api } from "../api";
import { Button } from "../ui";

function Glyph({ item }: { item: SubtaskItem }) {
  if (item.verified) return <CheckCircle2 size={14} className="shrink-0 text-ok" />;
  if (item.done) return <Check size={14} className="shrink-0 text-defer" />;
  return <Circle size={14} className="shrink-0 text-muted" />;
}

// A preserved partial (P-0081, R3-D3) awaits an operator disposition.
const isPendingPreserved = (s: SubtaskItem) =>
  !!s.preserved && s.preserved.disposition == null;

// The disposition row for a preserved partial: a cancelled turn captured this
// item's expected artifact, so it is neither verified (cancellation is not
// completion) nor missing (the artifact exists). The operator decides.
function PreservedRow({
  itemId,
  sub,
  onChanged,
}: {
  itemId: number;
  sub: SubtaskItem;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const dispose = async (d: "accept" | "discard" | "reopen") => {
    setBusy(true);
    setErr(null);
    try {
      await api.disposePreservedPartial(itemId, sub.id, d);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not update.");
      setBusy(false);
    }
  };
  return (
    <div className="mt-1 ml-5 rounded border border-defer/40 bg-defer/10 px-2 py-1.5">
      <div className="text-[11px] text-defer">
        preserved partial — a cancelled turn saved this output; it is not a
        verified completion. What should happen to it?
      </div>
      <div className="mt-1 flex items-center gap-2">
        <button
          className="font-mono text-[11px] text-ok hover:underline disabled:opacity-50"
          disabled={busy}
          title="Accept the partial as delivered (marked done, not verified)"
          onClick={() => dispose("accept")}
        >
          accept
        </button>
        <button
          className="font-mono text-[11px] text-bad hover:underline disabled:opacity-50"
          disabled={busy}
          title="Discard the partial and reopen this obligation"
          onClick={() => dispose("discard")}
        >
          discard
        </button>
        <button
          className="font-mono text-[11px] text-brand hover:underline disabled:opacity-50"
          disabled={busy}
          title="Keep the partial on record but redo the work"
          onClick={() => dispose("reopen")}
        >
          reopen
        </button>
        {err && <span className="text-[11px] text-bad">{err}</span>}
      </div>
    </div>
  );
}

export default function SubtaskChecklist({
  item,
  onChanged,
}: {
  item: WorkItem;
  onChanged: () => void;
}) {
  const items = item.subtasks?.items ?? [];
  const progress = item.subtask_progress;
  const proposed = items.filter((s) => s.status === "proposed");
  const confirmed = items.filter((s) => s.status === "confirmed");

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<SubtaskItemInput[]>([]);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const startEdit = () => {
    // Seed the editor from current items; a proposed item becomes a confirmed
    // candidate the operator can keep (edit) or drop (remove the row).
    setDraft(
      items.map((s) => ({
        id: s.id,
        label: s.label,
        expected: s.expected,
        status: s.status === "proposed" ? "confirmed" : s.status,
        done: s.done,
      })),
    );
    setEditing(true);
    setErr(null);
  };

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      await api.setSubtasks(
        item.id,
        draft.filter((d) => d.label.trim()),
      );
      setEditing(false);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save sub-tasks.");
    } finally {
      setSaving(false);
    }
  };

  const patch = (i: number, p: Partial<SubtaskItemInput>) =>
    setDraft((d) => d.map((row, idx) => (idx === i ? { ...row, ...p } : row)));

  // ── Read view ──────────────────────────────────────────────────────────────
  if (!editing) {
    if (items.length === 0) {
      return (
        <div className="mt-2">
          <Button variant="ghost" size="sm" icon={<Plus size={12} />}
            onClick={() => { setDraft([{ label: "", expected: "", status: "confirmed" }]); setEditing(true); }}>
            Add sub-tasks
          </Button>
        </div>
      );
    }
    return (
      <div className="mt-2 space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">sub-tasks</span>
          {progress && (
            <span className="font-mono text-[11px] text-muted">
              <span className="text-ok">{progress.verified} verified</span>
              {progress.claimed > 0 && <span className="text-defer"> · {progress.claimed} claimed</span>}
              {" · "}{progress.total} confirmed
              {progress.proposed > 0 && (
                <span className="text-warn"> · {progress.proposed} proposed — review</span>
              )}
            </span>
          )}
          <button
            className="ml-auto font-mono text-[11px] text-brand hover:underline"
            onClick={startEdit}
          >
            {proposed.length > 0 ? "review" : "edit"}
          </button>
        </div>
        {/* Grounded progress bar over confirmed items. */}
        {progress && progress.total > 0 && (
          <div className="flex h-1.5 overflow-hidden rounded-full bg-edge">
            <div className="bg-ok" style={{ width: `${(progress.verified / progress.total) * 100}%` }} />
            <div className="bg-defer" style={{ width: `${(progress.claimed / progress.total) * 100}%` }} />
          </div>
        )}
        <ul className="space-y-1">
          {[...proposed, ...confirmed].map((s) => (
            <li key={s.id} className="text-xs">
              <div className="flex items-center gap-2">
                {s.status === "proposed" ? (
                  <Circle size={14} className="shrink-0 text-warn" />
                ) : (
                  <Glyph item={s} />
                )}
                <span
                  className={s.verified ? "text-ink" : "text-muted"}
                  title={
                    s.verified && s.verified_by
                      ? `verified by ${s.verified_by.lane} ${s.verified_by.ref}`
                      : undefined
                  }
                >
                  {s.label}
                </span>
                {s.expected && (
                  <span className="font-mono text-[10px] text-muted/70">{s.expected}</span>
                )}
                {s.status === "proposed" && <span className="font-mono text-[10px] text-warn">proposed</span>}
              </div>
              {isPendingPreserved(s) && (
                <PreservedRow itemId={item.id} sub={s} onChanged={onChanged} />
              )}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  // ── Edit view (confirm / modify) ─────────────────────────────────────────────
  return (
    <div className="mt-2 space-y-2 rounded-lg border border-edge bg-edge/10 p-2">
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted">
        confirm / modify sub-tasks · an <span className="text-ink">expected</span> path or glob makes an
        item auto-verified when it lands
      </div>
      {draft.map((row, i) => (
        <div key={row.id ?? i} className="flex items-center gap-1.5">
          <input
            className="h-8 min-w-0 flex-1 rounded border border-edge bg-bg px-2 text-xs text-ink"
            placeholder="what this sub-task delivers"
            value={row.label}
            onChange={(e) => patch(i, { label: e.target.value })}
          />
          <input
            className="h-8 w-40 rounded border border-edge bg-bg px-2 font-mono text-[11px] text-ink"
            placeholder="expected path/glob"
            value={row.expected ?? ""}
            onChange={(e) => patch(i, { expected: e.target.value || null })}
          />
          {!row.expected && (
            <label className="flex items-center gap-1 text-[10px] text-muted" title="mark done (unverified)">
              <input type="checkbox" checked={!!row.done} onChange={(e) => patch(i, { done: e.target.checked })} />
              done
            </label>
          )}
          <button
            className="shrink-0 text-muted hover:text-bad"
            aria-label="remove sub-task"
            onClick={() => setDraft((d) => d.filter((_, idx) => idx !== i))}
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
      {err && <p className="text-[11px] text-bad">{err}</p>}
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" icon={<Plus size={12} />}
          onClick={() => setDraft((d) => [...d, { label: "", expected: "", status: "confirmed" }])}>
          Add
        </Button>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
          <Button variant="primary" size="sm" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>
    </div>
  );
}
