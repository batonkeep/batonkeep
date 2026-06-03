// format.ts — small presentation helpers shared across components.
import type { RunStatus } from "./types";

/** Ensure a naive ISO string from the backend is parsed as UTC, not local time. */
function asUTC(iso: string): Date {
  // If no timezone designator, the backend stored UTC — tag it explicitly.
  if (!iso.endsWith("Z") && !iso.match(/[+-]\d{2}:\d{2}$/)) {
    return new Date(iso + "Z");
  }
  return new Date(iso);
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${rem}s`;
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd == null) return "—";
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

export function fmtPct(frac: number | null | undefined): string {
  if (frac == null) return "—";
  return `${Math.round(frac * 100)}%`;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = asUTC(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = asUTC(iso).getTime();
  const diff = Date.now() - then;
  const abs = Math.abs(diff);
  const fut = diff < 0;
  const mk = (v: number, u: string) => `${fut ? "in " : ""}${v}${u}${fut ? "" : " ago"}`;
  if (abs < 60_000) return fut ? "soon" : "just now";
  if (abs < 3_600_000) return mk(Math.round(abs / 60_000), "m");
  if (abs < 86_400_000) return mk(Math.round(abs / 3_600_000), "h");
  return mk(Math.round(abs / 86_400_000), "d");
}

/** Seconds remaining until an ISO timestamp, formatted mm:ss / h m. */
export function countdown(iso: string | null | undefined, now: number): string {
  if (!iso) return "—";
  const ms = asUTC(iso).getTime() - now;
  if (ms <= 0) return "ready";
  const s = Math.floor(ms / 1000);
  if (s < 3600) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

const CRON_PRESETS: Record<string, string> = {
  "0 7 * * *": "Daily at 07:00",
  "30 6 * * 1-5": "Weekdays at 06:30",
  "0 * * * *": "Hourly",
  "0 0 * * *": "Daily at midnight",
};

export function humanizeSchedule(
  kind: string,
  expr: string | null | undefined,
  tz?: string | null
): string {
  if (kind === "none" || !expr) return "Manual only";
  if (kind === "interval") {
    const sec = parseInt(expr, 10);
    if (isNaN(sec)) return `Every ${expr}`;
    if (sec % 86400 === 0) return `Every ${sec / 86400}d`;
    if (sec % 3600 === 0) return `Every ${sec / 3600}h`;
    if (sec % 60 === 0) return `Every ${sec / 60}m`;
    return `Every ${sec}s`;
  }
  if (kind === "cron") {
    const base = CRON_PRESETS[expr] || `cron: ${expr}`;
    // Surface the timezone (cron is interpreted there, not UTC) when it's not UTC.
    return tz && tz !== "UTC" ? `${base} · ${tz}` : base;
  }
  return expr;
}

/** Basic 5-field crontab validation (min hour dom mon dow). */
export function isValidCron(expr: string): boolean {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  const ranges = [
    [0, 59],
    [0, 23],
    [1, 31],
    [1, 12],
    [0, 7],
  ];
  return parts.every((p, i) => {
    if (p === "*") return true;
    const [lo, hi] = ranges[i];
    // accept */n, a-b, a,b, and plain numbers within range
    return p.split(",").every((tok) => {
      const step = tok.split("/");
      const base = step[0];
      if (base === "*") return true;
      const rng = base.split("-").map(Number);
      return rng.every((n) => !isNaN(n) && n >= lo && n <= hi);
    });
  });
}

export const STATUS_META: Record<RunStatus, { label: string; dot: string; text: string }> = {
  queued: { label: "Queued", dot: "bg-muted", text: "text-muted" },
  planning: { label: "Planning", dot: "bg-live animate-pulse-live", text: "text-live" },
  running: { label: "Running", dot: "bg-live animate-pulse-live", text: "text-live" },
  succeeded: { label: "Succeeded", dot: "bg-ok", text: "text-ok" },
  failed: { label: "Failed", dot: "bg-bad", text: "text-bad" },
  deferred: { label: "Deferred", dot: "bg-defer", text: "text-defer" },
  cancelled: { label: "Cancelled", dot: "bg-muted", text: "text-muted" },
};
