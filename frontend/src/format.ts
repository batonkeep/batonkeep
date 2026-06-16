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

// Compact count, e.g. 850 → "850", 2000 → "2k", 15647 → "15.6k", 2_300_000 → "2.3M".
// Keeps one decimal in the k/M range (dropping a trailing ".0"); keep the exact value
// in a `title` where precision matters.
export function fmtCount(n: number | null | undefined): string {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs < 1000) return `${n}`;
  const strip = (v: number) => v.toFixed(1).replace(/\.0$/, "");
  if (abs < 1_000_000) return `${strip(n / 1000)}k`;
  return `${strip(n / 1_000_000)}M`;
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
  "0 * * * *":      "Every hour",
  "0 0 * * *":      "Daily at midnight",
  "0 7 * * *":      "Daily at 07:00",
  "30 6 * * 1-5":   "Weekdays at 06:30",
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

// ---------------------------------------------------------------------------
// Cron picker helpers
// ---------------------------------------------------------------------------

export type CronPresetId =
  | "hourly"
  | "daily"
  | "weekdays"
  | "weekly"
  | "monthly"
  | "custom";

export const CRON_PRESET_LABELS: Record<CronPresetId, string> = {
  hourly:   "Every hour",
  daily:    "Every day",
  weekdays: "Every weekday (Mon–Fri)",
  weekly:   "Every week",
  monthly:  "Every month",
  custom:   "Custom / Advanced",
};

const DAYS_OF_WEEK = [
  { value: "0", label: "Sunday" },
  { value: "1", label: "Monday" },
  { value: "2", label: "Tuesday" },
  { value: "3", label: "Wednesday" },
  { value: "4", label: "Thursday" },
  { value: "5", label: "Friday" },
  { value: "6", label: "Saturday" },
];
export { DAYS_OF_WEEK };

/** Build a cron string from preset + selected hour/min/day values. */
export function buildCronFromPreset(
  preset: CronPresetId,
  hour: number,
  minute: number,
  dayOfWeek: number, // 0=Sun … 6=Sat
  dayOfMonth: number // 1–28
): string {
  const h = String(hour);
  const m = String(minute);
  switch (preset) {
    case "hourly":   return `${m} * * * *`;
    case "daily":    return `${m} ${h} * * *`;
    case "weekdays": return `${m} ${h} * * 1-5`;
    case "weekly":   return `${m} ${h} * * ${dayOfWeek}`;
    case "monthly":  return `${m} ${h} ${dayOfMonth} * *`;
    default:         return "";
  }
}

/** Parse a cron string back to a preset ID + field values (best-effort). */
export function cronToPreset(expr: string): {
  preset: CronPresetId;
  hour: number;
  minute: number;
  dayOfWeek: number;
  dayOfMonth: number;
} {
  const defaults = { preset: "custom" as CronPresetId, hour: 9, minute: 0, dayOfWeek: 1, dayOfMonth: 1 };
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return defaults;
  const [min, hr, dom, mon, dow] = parts;
  const m = parseInt(min, 10);
  const h = parseInt(hr, 10);

  if (isNaN(m) || isNaN(h) || mon !== "*") return defaults;

  // hourly: M * * * *
  if (hr === "*" && dom === "*" && dow === "*")
    return { ...defaults, preset: "hourly", minute: isNaN(m) ? 0 : m };

  // daily: M H * * *
  if (dom === "*" && dow === "*")
    return { ...defaults, preset: "daily", hour: h, minute: m };

  // weekdays: M H * * 1-5
  if (dom === "*" && dow === "1-5")
    return { ...defaults, preset: "weekdays", hour: h, minute: m };

  // weekly: M H * * N  (single digit)
  if (dom === "*" && /^[0-6]$/.test(dow))
    return { ...defaults, preset: "weekly", hour: h, minute: m, dayOfWeek: parseInt(dow, 10) };

  // monthly: M H D * *
  if (dow === "*" && /^\d+$/.test(dom)) {
    const d = parseInt(dom, 10);
    return { ...defaults, preset: "monthly", hour: h, minute: m, dayOfMonth: isNaN(d) ? 1 : d };
  }

  return defaults;
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
