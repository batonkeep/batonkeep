// CockpitPanel.tsx — the operator cockpit (D-0022 Task A, audience A).
// A local-first, sovereign view of the user's *own* work: spend, run outcomes,
// latency, failovers, errors-by-class, and build/session activity over a window.
// Reads /api/cockpit; nothing here is shared (that is audience B, gated on managed).
import { useCallback, useEffect, useState } from "react";
import { Activity, RefreshCw, ShieldCheck } from "lucide-react";
import { api } from "../api";
import type { Cockpit } from "../types";
import { fmtCost, fmtDuration, fmtPct } from "../format";
import { Badge, Card, Select, StatusDot } from "../ui";

const WINDOWS = [
  { v: 1, label: "24h" },
  { v: 7, label: "7d" },
  { v: 30, label: "30d" },
];

// Error-class → static text colour. Structural classes only (content-free by
// design). Static strings so Tailwind's JIT keeps them.
const ERROR_CLASS_COLOR: Record<string, string> = {
  rate_limited: "text-defer",
  cooling: "text-defer",
  unavailable: "text-defer",
  interrupted: "text-defer",
  error: "text-bad",
  other: "text-ink",
};
const errorColor = (k: string) => ERROR_CLASS_COLOR[k] ?? "text-ink";

function Stat({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: string;
}) {
  return (
    <Card className="min-w-[7.5rem] flex-1 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`font-mono text-lg font-semibold leading-tight ${tone || "text-ink"}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
    </Card>
  );
}

function Section({ title, hint, children }: {
  title: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <Card className="p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="font-mono text-sm font-semibold text-ink">{title}</h3>
        {hint && <span className="text-[11px] text-muted">{hint}</span>}
      </div>
      {children}
    </Card>
  );
}

// A simple label→count row list (sorted desc), shared by the breakdown blocks.
function Breakdown({ data, empty, tone }: {
  data: Record<string, number>; empty: string; tone?: (k: string) => string;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <p className="text-[11px] text-muted">{empty}</p>;
  return (
    <div className="space-y-1.5">
      {entries.map(([k, n]) => (
        <div key={k} className="flex items-center justify-between text-sm">
          <span className={`font-mono ${tone ? tone(k) : "text-ink"}`}>{k}</span>
          <span className="font-mono text-muted">{n}</span>
        </div>
      ))}
    </div>
  );
}

export default function CockpitPanel() {
  const [windowDays, setWindowDays] = useState(7);
  const [data, setData] = useState<Cockpit | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (days: number) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getCockpit(days));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load cockpit");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(windowDays); }, [load, windowDays]);

  const runs = data?.runs;
  const lat = data?.latency;
  const rel = data?.reliability;
  const act = data?.activity;

  return (
    <div className="space-y-4">
      {/* Header — window selector */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 font-mono text-lg font-semibold text-ink">
            <Activity size={18} className="text-brand" /> Analytics
          </h2>
          <p className="mt-0.5 text-[11px] text-muted">Operational telemetry for your deployment</p>
        </div>
        <div className="flex items-center gap-2">
          <Select
            value={windowDays}
            onChange={(e) => setWindowDays(Number(e.target.value))}
            className="w-28"
          >
            {WINDOWS.map((w) => <option key={w.v} value={w.v}>Last {w.label}</option>)}
          </Select>
          <button
            onClick={() => load(windowDays)}
            className="rounded-lg border border-edge p-2.5 text-muted hover:text-ink"
            title="Refresh"
          >
            <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* Sovereignty badge — prominent callout (D-0027 item 6, D-0022 positioning asset) */}
      <div className="flex items-start gap-3 rounded-lg border border-ok/30 bg-ok/5 px-4 py-3">
        <ShieldCheck size={18} className="mt-0.5 shrink-0 text-ok" />
        <div>
          <p className="text-sm font-semibold text-ok">Local-only telemetry</p>
          <p className="mt-0.5 text-[11px] text-muted">
            This data never leaves your deployment. No prompts, file contents, or identifiers
            are collected. Only structural metadata, stored on your own machine.
          </p>
        </div>
      </div>

      {error && <Badge tone="bad">{error}</Badge>}

      {data && (
        <>
          {/* Headline tiles. */}
          <div className="flex flex-wrap gap-2">
            <Stat label="Runs" value={String(runs!.total)}
              sub={runs!.total === 0
                ? "Run your first task to see data here"
                : `${runs!.active_runs} active · ${runs!.deferred_now} deferred`} />
            <Stat label="Success rate" value={fmtPct(runs!.success_rate)}
              tone={runs!.success_rate >= 0.8 ? "text-ok" : "text-ink"} />
            <Stat label="Error rate" value={fmtPct(runs!.error_rate)}
              tone={runs!.error_rate > 0.2 ? "text-bad" : "text-ink"} />
            <Stat label="Failover rate" value={fmtPct(rel!.failover_rate)}
              tone={rel!.failover_rate > 0.3 ? "text-defer" : "text-ink"} />
            <Stat label="Latency p50 / p95"
              value={`${fmtDuration(lat!.p50_ms)} / ${fmtDuration(lat!.p95_ms)}`}
              sub={`avg ${fmtDuration(lat!.avg_ms)} · n=${lat!.sample}`} />
            <Stat label="Spend (today / 7d)"
              value={fmtCost(data.spend.spend_today_usd)}
              sub={`7d ${fmtCost(data.spend.spend_7d_usd)}`}
              tone={data.spend.over_budget ? "text-bad" : "text-ink"} />
          </div>

          {data.spend.over_budget && (
            <Badge tone="bad">over budget — new runs degrading to free providers</Badge>
          )}

          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Section title="Runs by status" hint={`last ${data.window_days}d`}>
              <Breakdown data={runs!.by_status} empty="No runs in window." />
            </Section>

            <Section title="Runs by provider">
              <Breakdown data={runs!.by_provider} empty="No runs in window." />
            </Section>

            <Section title="Errors by class" hint="structural — no content">
              <Breakdown data={data.errors_by_class} empty="No failures." tone={errorColor} />
            </Section>

            <Section title="Reliability">
              <div className="space-y-1.5 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted">Retried runs</span>
                  <span className="font-mono">{rel!.retried_runs}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted">Budget-degraded</span>
                  <span className="font-mono">{rel!.budget_degraded_runs}</span>
                </div>
                <div className="mt-2 text-[11px] uppercase tracking-wider text-muted">Failover reasons</div>
                <Breakdown data={rel!.failover_reasons} empty="None." />
              </div>
            </Section>

            <Section title="Build / session activity">
              <div className="space-y-1.5 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted">Sessions</span>
                  <span className="font-mono">{act!.sessions_total}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted">Active / archived</span>
                  <span className="font-mono">{act!.sessions_active} / {act!.sessions_archived}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-muted">
                    <StatusDot tone="ok" /> Confidential
                  </span>
                  <span className="font-mono">{act!.sessions_confidential}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted">Turns</span>
                  <span className="font-mono">{act!.turns_total}</span>
                </div>
                <Breakdown data={act!.turns_by_status} empty="No turns." />
              </div>
            </Section>

            <Section title="Spend by provider" hint="today">
              <Breakdown
                data={Object.fromEntries(
                  Object.entries(data.spend.by_provider_today).map(([k, v]) => [k, Number(v.toFixed(4))])
                )}
                empty="No metered spend today."
              />
            </Section>
          </div>
        </>
      )}
    </div>
  );
}
