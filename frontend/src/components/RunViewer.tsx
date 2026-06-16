// RunViewer.tsx — live run inspector (§12): colour-coded event stream with candidate
// order + failover hops, elapsed timer, live token/cost counters, Markdown report via
// marked, Report/JSON/Raw tabs, downloads, and a deferred banner with requeue.
// D-track: composed from ui/ primitives (Button, Badge, Card, StatusDot, Tabs).
import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import { Ban, Download, RotateCw, X } from "lucide-react";
import type { Run, RunAsset, RunEvent } from "../types";
import { api } from "../api";
import { useRunEvents } from "../useLiveFeed";
import { STATUS_META, fmtCost, fmtCount, fmtDuration, fmtTime } from "../format";
import { Badge, Button, StatusDot, Tabs } from "../ui";

// Parse naive ISO backend timestamps as UTC before formatting.
function asUTC(iso: string): Date {
  if (!iso.endsWith("Z") && !iso.match(/[+-]\d{2}:\d{2}$/)) return new Date(iso + "Z");
  return new Date(iso);
}

interface Props {
  run: Run;
  taskName?: string;
  now: number;
  onRequeue: (run: Run) => void;
  onCancel: (run: Run) => void;
  onClose: () => void;
}

type Tab = "report" | "json" | "assets" | "raw";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const KIND_COLOR: Record<string, string> = {
  log: "text-muted",
  phase: "text-ink",
  tool: "text-brand",
  subagent: "text-brand",
  result: "text-ok",
  error: "text-bad",
  route: "text-live",
};

function routeLine(ev: RunEvent): string {
  const d = ev.data || {};
  if (Array.isArray(d.candidates)) {
    return `candidates: ${d.candidates.join(" › ")}${d.overflow_to ? `  (overflow → ${d.overflow_to})` : ""}`;
  }
  if (d.cooling && d.provider) {
    const next = Array.isArray(d.next) && d.next.length ? ` → next: ${d.next.join(" › ")}` : "";
    return `${d.provider} rate-limited, skipped${next}`;
  }
  if (Array.isArray(d.cooling)) {
    return `all cooling: ${d.cooling.join(", ")}${d.deferred_until ? ` · deferred until ${asUTC(d.deferred_until).toLocaleTimeString()}` : ""}`;
  }
  return ev.message || "route";
}

export default function RunViewer({ run, taskName, now, onRequeue, onCancel, onClose }: Props) {
  const { events, streamingText, seedEvents } = useRunEvents(run.id);
  const [tab, setTab] = useState<Tab>("report");
  const [reportMd, setReportMd] = useState<string | null>(null);
  const [jsonText, setJsonText] = useState<string | null>(null);
  const [assets, setAssets] = useState<RunAsset[]>([]);
  const streamRef = useRef<HTMLDivElement>(null);

  const meta = STATUS_META[run.status];
  const active = ["queued", "planning", "running"].includes(run.status);

  useEffect(() => {
    api.getRunEvents(run.id).then(seedEvents).catch(() => { });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id]);

  useEffect(() => {
    if (run.markdown_path) {
      fetch(api.outputUrl(run.id, "md")).then((r) => (r.ok ? r.text() : null)).then(setReportMd).catch(() => { });
    }
    if (run.json_path) {
      fetch(api.outputUrl(run.id, "json")).then((r) => (r.ok ? r.text() : null)).then(setJsonText).catch(() => { });
    }
  }, [run.id, run.markdown_path, run.json_path]);

  // Run assets (P-0050) — only meaningful once a run is terminal. Refetch when the
  // run finishes so a just-completed run shows its generated images.
  useEffect(() => {
    if (active) return;
    api.listRunAssets(run.id).then(setAssets).catch(() => { });
  }, [run.id, active]);

  useEffect(() => {
    if (tab === "raw" && streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
  }, [events, streamingText, tab]);

  const elapsedMs = useMemo(() => {
    if (!run.started_at) return null;
    // Backend timestamps are naive UTC — parse via asUTC, not new Date() (which
    // assumes local time and inflates elapsed by the tz offset, e.g. ~600min at UTC+10).
    const end = run.finished_at ? asUTC(run.finished_at).getTime() : now;
    return end - asUTC(run.started_at).getTime();
  }, [run.started_at, run.finished_at, now]);

  const runTabs = useMemo(() => {
    const t: { id: Tab; label: string }[] = [
      { id: "report", label: "Report" },
      { id: "json", label: "JSON" },
    ];
    if (assets.length) t.push({ id: "assets", label: `Assets · ${assets.length}` });
    t.push({ id: "raw", label: "Raw" });
    return t as readonly { id: Tab; label: string }[];
  }, [assets.length]);

  const reportHtml = useMemo(() => {
    const src = reportMd ?? (active ? streamingText : run.summary ?? "");
    if (!src) return "";
    const html = marked.parse(src, { async: false }) as string;
    // Reports may reference captured assets by relative path (assets/… or data/…,
    // e.g. an agy-generated image pulled in at capture time). Resolve those against
    // this run's asset route so they render inline. Absolute/http srcs are untouched.
    const base = api.runAssetBase(run.id);
    return html.replace(/(src|href)="(assets|data)\//g, `$1="${base}$2/`);
  }, [reportMd, streamingText, active, run.summary, run.id]);

  return (
    <div className="flex h-full flex-col rounded-xl border border-edge bg-panel/90">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 border-b border-edge px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <StatusDot
              tone={meta.dot?.includes("ok") ? "ok" : meta.dot?.includes("bad") ? "bad" : meta.dot?.includes("live") ? "live" : meta.dot?.includes("defer") ? "defer" : "neutral"}
              pulse={active}
            />
            <h2 className="truncate font-mono text-sm font-semibold text-ink">
              {taskName || `Run #${run.id}`}
            </h2>
            <Badge
              tone={meta.dot?.includes("ok") ? "ok" : meta.dot?.includes("bad") ? "bad" : meta.dot?.includes("live") ? "live" : meta.dot?.includes("defer") ? "defer" : "neutral"}
            >
              {meta.label}
            </Badge>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-muted">
            <span>run #{run.id}</span>
            {run.provider && <span className="text-ink">{run.provider}{run.model ? ` · ${run.model}` : ""}</span>}
            {run.started_at && <span title={run.started_at}>{fmtTime(run.started_at)}</span>}
            <span>elapsed {fmtDuration(run.finished_at ? run.duration_ms : elapsedMs)}</span>
            <span
              className="text-brand"
              title={`${(run.tokens_in + run.tokens_out).toLocaleString()} tokens (${run.tokens_in.toLocaleString()} in · ${run.tokens_out.toLocaleString()} out)`}
            >
              {fmtCount(run.tokens_in + run.tokens_out)} tok
            </span>
            <span className="text-brand">{fmtCost(run.cost_usd)}</span>
            {run.overflow_used && <Badge tone="defer">overflow</Badge>}
          </div>
        </div>
        <div className="flex items-center gap-1">
          {active && (
            <Button variant="ghost" size="sm" icon={<Ban size={13} />} onClick={() => onCancel(run)}
              className="text-muted hover:text-bad">
              Cancel
            </Button>
          )}
          <Button variant="ghost" size="sm" className="px-1.5" onClick={onClose}
            icon={<X size={18} />} />
        </div>
      </div>

      {/* Deferred banner */}
      {run.status === "deferred" && (
        <div className="flex items-center justify-between gap-2 border-b border-defer/30 bg-defer/10 px-4 py-2.5">
          <div className="text-xs text-defer">
            {run.error || "All candidates were cooling down."}
            {run.deferred_until && <span> · resumes {asUTC(run.deferred_until).toLocaleString()}</span>}
          </div>
          <Button variant="outline" size="sm" icon={<RotateCw size={13} />}
            onClick={() => onRequeue(run)}
            className="border-defer/50 text-defer hover:bg-defer/10">
            Run now
          </Button>
        </div>
      )}

      {/* Candidate order + failover hops */}
      {run.attempts && run.attempts.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-edge px-4 py-2 font-mono text-[11px]">
          <span className="text-muted">hops:</span>
          {run.attempts.map((a, i) => {
            const tone =
              a.outcome === "success" ? "ok" :
              a.outcome === "rate_limited" ? "defer" :
              a.outcome === "error" || a.outcome === "unavailable" ? "bad" : "neutral";
            return (
              <Badge key={i} tone={tone as "ok" | "defer" | "bad" | "neutral"}>
                {a.provider} · {a.outcome}
              </Badge>
            );
          })}
        </div>
      )}

      {/* Tabs + downloads */}
      <div className="flex items-center gap-2 border-b border-edge px-3 pt-2">
        <Tabs tabs={runTabs} active={tab} onChange={setTab} />
        <div className="ml-auto flex items-center gap-2 pb-1">
          {run.markdown_path && (
            <a href={api.outputUrl(run.id, "md")} target="_blank" rel="noopener noreferrer"
              className="flex items-center gap-1 text-xs text-muted hover:text-ink">
              <Download size={13} /> md
            </a>
          )}
          {run.json_path && (
            <a href={api.outputUrl(run.id, "json")} target="_blank" rel="noopener noreferrer"
              className="flex items-center gap-1 text-xs text-muted hover:text-ink">
              <Download size={13} /> json
            </a>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4">
        {tab === "report" && (
          reportHtml ? (
            <div className="markdown" dangerouslySetInnerHTML={{ __html: reportHtml }} />
          ) : (
            <div className="text-sm text-muted">
              {active ? "Awaiting output…" : run.error ? <span className="text-bad">{run.error}</span> : "No report."}
              {active && <span className="ml-1 inline-block h-3 w-1.5 animate-pulse-live bg-live align-middle" />}
            </div>
          )
        )}

        {tab === "json" && (
          jsonText ? (
            <pre className="overflow-x-auto rounded-lg border border-edge bg-base p-3 text-xs text-ink">{jsonText}</pre>
          ) : (
            <div className="text-sm text-muted">No JSON output for this run.</div>
          )
        )}

        {tab === "assets" && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {assets.map((a) => {
              const mime = a.mime || "";
              const kind = mime.startsWith("image/") ? "image"
                : mime.startsWith("video/") ? "video"
                : mime.startsWith("audio/") ? "audio" : "file";
              const src = api.runAssetUrl(run.id, a.rel_path);
              return (
                <a
                  key={a.id}
                  href={src}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group flex flex-col overflow-hidden rounded-lg border border-edge bg-base hover:border-brand"
                >
                  {kind === "image" ? (
                    <img src={src} alt={a.rel_path} className="aspect-square w-full object-cover" />
                  ) : kind === "video" ? (
                    <video src={src} className="aspect-square w-full object-cover" muted playsInline controls />
                  ) : kind === "audio" ? (
                    <div className="flex aspect-square w-full items-center justify-center p-2">
                      <audio src={src} controls className="w-full" onClick={(e) => e.preventDefault()} />
                    </div>
                  ) : (
                    <div className="flex aspect-square w-full items-center justify-center text-3xl text-muted">
                      <Download size={28} />
                    </div>
                  )}
                  <div className="flex items-center justify-between gap-1 px-2 py-1.5 font-mono text-[10px] text-muted">
                    <span className="truncate text-ink/80" title={a.rel_path}>
                      {a.rel_path.replace(/^(assets|data)\//, "")}
                    </span>
                    <span className="shrink-0">{fmtBytes(a.bytes)}</span>
                  </div>
                </a>
              );
            })}
          </div>
        )}

        {tab === "raw" && (
          <div ref={streamRef} className="space-y-1 font-mono text-xs">
            {events.length === 0 && !streamingText && <div className="text-muted">No events yet.</div>}
            {events.map((ev, i) => (
              <div key={`${ev.seq}-${i}`} className="flex gap-2">
                <span className="w-10 shrink-0 text-right text-muted">{ev.seq}</span>
                <span className={`w-16 shrink-0 ${KIND_COLOR[ev.kind] || "text-muted"}`}>{ev.kind}</span>
                <span className="flex-1 break-words text-ink/90">
                  {ev.kind === "route" ? routeLine(ev) : ev.message || ev.phase || ""}
                </span>
              </div>
            ))}
            {active && streamingText && (
              <div className="flex gap-2">
                <span className="w-10 shrink-0" />
                <span className="w-16 shrink-0 text-live">token</span>
                <span className="flex-1 whitespace-pre-wrap text-ink/70">{streamingText.slice(-600)}</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
