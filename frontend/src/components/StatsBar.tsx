// StatsBar.tsx — top metrics strip (§12): runs today, success rate, avg duration,
// runs by provider, failover rate, # deferred, active-runs pulse + runs/day sparkline.
// D-track: Tile is now a thin wrapper over Card + StatusDot.
import { Stats } from "../types";
import { fmtCost, fmtDuration, fmtPct } from "../format";
import Sparkline from "./Sparkline";
import { Card, StatusDot } from "../ui";

interface Props {
  stats: Stats | null;
  sparkData: number[];
}

function Tile({ label, value, brand, children }: {
  label: string; value?: string; brand?: string; children?: React.ReactNode;
}) {
  return (
    <Card className="min-w-[7.5rem] flex-1 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      {value != null && (
        <div className={`font-mono text-lg font-semibold leading-tight ${brand || "text-ink"}`}>{value}</div>
      )}
      {children}
    </Card>
  );
}

export default function StatsBar({ stats, sparkData }: Props) {
  const byProvider = stats?.runs_by_provider || {};
  const providerEntries = Object.entries(byProvider).sort((a, b) => b[1] - a[1]);

  return (
    <div className="stagger flex flex-wrap gap-2">
      <Tile label="Runs today" value={String(stats?.runs_today ?? 0)} />
      <Tile label="Success rate" value={fmtPct(stats?.success_rate)}
        brand={stats && stats.success_rate >= 0.8 ? "text-ok" : "text-ink"} />
      <Tile label="Avg duration" value={fmtDuration(stats?.avg_duration_ms ?? null)} />
      <Tile label="Failover rate" value={fmtPct(stats?.failover_rate)}
        brand={stats && stats.failover_rate > 0.3 ? "text-defer" : "text-ink"} />
      <Tile label="Deferred" value={String(stats?.deferred_now ?? 0)}
        brand={stats && stats.deferred_now > 0 ? "text-defer" : "text-ink"} />
      <Tile label="Cost today" value={fmtCost(stats?.cost_today_usd)} />

      <Tile label="Active">
        <div className="flex items-center gap-2">
          <span className="font-mono text-lg font-semibold leading-tight text-live">
            {stats?.active_runs ?? 0}
          </span>
          {!!stats?.active_runs && <StatusDot tone="live" pulse size={8} />}
        </div>
      </Tile>

      <Tile label="Runs / day">
        <div className="pt-1"><Sparkline data={sparkData} /></div>
      </Tile>

      {providerEntries.length > 0 && (
        <Tile label="By provider">
          <div className="flex flex-col gap-0.5 pt-0.5">
            {providerEntries.slice(0, 4).map(([name, count]) => (
              <div key={name} className="flex items-center justify-between gap-2 font-mono text-xs">
                <span className="text-muted">{name}</span>
                <span className="text-ink">{count}</span>
              </div>
            ))}
          </div>
        </Tile>
      )}
    </div>
  );
}
