// SessionView.tsx — the build-session work surface (M1.2). Left: session list +
// new-session form. Center: chat input with a provider switcher + turn history
// with the live event stream. Right: the live preview <iframe> pointed at the
// session's token-authenticated workspace, refreshed when a turn completes.
// D-track: composed from ui/ primitives (Button, Badge, Card, StatusDot, Select).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import { Activity, Check, ChevronRight, Loader2, Pencil, Plus, RefreshCw, Send, X } from "lucide-react";
import type { ProviderHealth, Session, SessionTurn } from "../types";
import { api } from "../api";
import { useSessionEvents, type SessionEvent } from "../useLiveFeed";
import { fmtTime } from "../format";
import { Badge, Button, Card, Select, StatusDot, type Tone } from "../ui";

function renderMarkdown(src: string): string {
  return marked.parse(src, { async: false }) as string;
}

// The live feed shows a curated set of meaningful events by default. `log` frames
// carry internal scaffolding (the assembled turn-context prompt, CLI launch flags
// like --dangerously-skip-permissions, raw end-of-stream markers) that would read
// as unsafe/noisy to the user — those are only shown when "raw" is expanded.
const CURATED_KINDS = new Set(["phase", "tool", "subagent", "result", "route", "error"]);
function isCurated(ev: SessionEvent): boolean {
  return CURATED_KINDS.has(ev.kind);
}

// Animated "generating…" line shown while a turn is in flight. Surfaces the most
// recent curated step so the user sees forward progress, not a frozen spinner.
function GeneratingIndicator({ latest }: { latest?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 text-sm text-live">
      <Loader2 size={14} className="animate-spin" />
      <span>Generating…</span>
      {latest && <span className="truncate font-mono text-[11px] text-muted">{latest}</span>}
    </div>
  );
}

interface Props {
  sessions: Session[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onSessionsChanged: () => void; // reload the session list in the parent
  providers: ProviderHealth[];
}

const TURN_TONE: Record<SessionTurn["status"], Tone> = {
  running: "live",
  succeeded: "ok",
  failed: "bad",
};

const KIND_COLOR: Record<string, string> = {
  log: "text-muted",
  phase: "text-ink",
  tool: "text-amber",
  subagent: "text-amber",
  result: "text-ok",
  error: "text-bad",
  route: "text-live",
};

export default function SessionView({
  sessions,
  selectedId,
  onSelect,
  onSessionsChanged,
  providers,
}: Props) {
  const [detail, setDetail] = useState<Session | null>(null);
  const [turns, setTurns] = useState<SessionTurn[]>([]);
  const [message, setMessage] = useState("");
  const [providerSwitch, setProviderSwitch] = useState("");
  const [sending, setSending] = useState(false);
  const [creating, setCreating] = useState(false);
  const [previewNonce, setPreviewNonce] = useState(0);
  const [rawOpen, setRawOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [titleDraft, setTitleDraft] = useState<string | null>(null); // non-null = editing
  const [pendingMessage, setPendingMessage] = useState<string | null>(null); // optimistic turn
  const [sendError, setSendError] = useState<string | null>(null);

  const { events, streamingText, lastTurn } = useSessionEvents(selectedId);
  const streamRef = useRef<HTMLDivElement>(null);

  // Distinct provider instance ids for the switcher (grouped by what's healthy).
  const providerIds = useMemo(
    () => Array.from(new Set(providers.map((p) => p.name))),
    [providers]
  );

  const loadTurns = useCallback(() => {
    if (!selectedId) return;
    api.listTurns(selectedId).then(setTurns).catch(() => {});
  }, [selectedId]);

  // Load the selected session detail (for the preview token) + its turn history.
  useEffect(() => {
    setDetail(null);
    setTurns([]);
    setProviderSwitch("");
    setPreviewNonce(0);
    setRawOpen(false);
    setActivityOpen(false);
    setTitleDraft(null);
    setPendingMessage(null);
    setSendError(null);
    if (!selectedId) return;
    api.getSession(selectedId).then(setDetail).catch(() => {});
    loadTurns();
  }, [selectedId, loadTurns]);

  // When a turn finishes, refresh the turn list and bust the preview iframe so it
  // reflects the latest workspace edits (Cache-Control: no-store on the backend).
  useEffect(() => {
    if (lastTurn && lastTurn.status !== "running") {
      loadTurns();
      setPreviewNonce((n) => n + 1);
    }
  }, [lastTurn, loadTurns]);

  // Auto-scroll the chat to the latest content.
  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
  }, [events, streamingText, turns, pendingMessage]);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const s = await api.createSession({ title: "Untitled session" });
      onSessionsChanged();
      onSelect(s.id);
    } finally {
      setCreating(false);
    }
  };

  const handleRename = async () => {
    const next = (titleDraft ?? "").trim();
    if (!selectedId || !next || next === detail?.title) {
      setTitleDraft(null);
      return;
    }
    const updated = await api.updateSession(selectedId, { title: next });
    setDetail(updated);
    setTitleDraft(null);
    onSessionsChanged();
  };

  const handleSend = async () => {
    const text = message.trim();
    if (!selectedId || !text || sending) return;
    // Optimistic: show the user's message and clear the input immediately, rather
    // than waiting for the (potentially long) turn to complete server-side.
    setMessage("");
    setSendError(null);
    setPendingMessage(text);
    setSending(true);
    try {
      await api.createTurn(selectedId, {
        message: text,
        provider: providerSwitch || undefined,
      });
      loadTurns();
      onSessionsChanged(); // session.provider may have switched
    } catch (err) {
      // Surface the failure and restore the message so it isn't lost.
      setSendError(err instanceof Error ? err.message : "Failed to send message");
      setMessage(text);
    } finally {
      setPendingMessage(null);
      setSending(false);
    }
  };

  // Cache-bust with a query param so relative asset links in the page still resolve
  // against the token base; the backend ignores it (Cache-Control: no-store anyway).
  const previewSrc =
    detail && detail.preview_token
      ? `${api.previewUrl(detail.id, detail.preview_token)}${
          previewNonce ? `?_=${previewNonce}` : ""
        }`
      : null;

  const turnRunning = lastTurn?.status === "running" || sending;
  const curatedEvents = useMemo(() => events.filter(isCurated), [events]);
  const hiddenCount = events.length - curatedEvents.length;
  const shownEvents = rawOpen ? events : curatedEvents;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[15rem_minmax(0,1fr)_minmax(0,1fr)]">
      {/* ── Session list ───────────────────────────────────────────────── */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="font-mono text-xs uppercase tracking-widest text-muted">Sessions</span>
          <Button
            variant="outline"
            size="sm"
            className="px-2"
            icon={<Plus size={13} />}
            onClick={handleCreate}
            disabled={creating}
            title="New build session"
          />
        </div>
        {sessions.length === 0 && (
          <div className="rounded-lg border border-dashed border-edge p-4 text-center text-xs text-muted">
            No sessions yet.
          </div>
        )}
        {sessions.map((s) => {
          const active = s.id === selectedId;
          return (
            <button
              key={s.id}
              onClick={() => onSelect(s.id)}
              className={`block w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                active ? "border-amber/50 bg-amber/10" : "border-edge bg-panel/60 hover:border-amber/30"
              }`}
            >
              <span className="block truncate font-mono text-sm text-ink">{s.title}</span>
              <span className="font-mono text-[11px] text-muted">
                {s.provider ?? "—"} · {fmtTime(s.updated_at)}
              </span>
            </button>
          );
        })}
      </div>

      {/* ── Chat: provider switcher, input, turn history + live events ──── */}
      <Card className="flex h-[70vh] flex-col p-0">
        {!selectedId ? (
          <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted">
            Select a session, or create one to start building.
          </div>
        ) : (
          <>
            {/* Editable session title */}
            <div className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
              {titleDraft !== null ? (
                <>
                  <input
                    autoFocus
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleRename();
                      if (e.key === "Escape") setTitleDraft(null);
                    }}
                    className="flex-1 rounded-md border border-edge bg-base px-2 py-1 font-mono text-sm text-ink focus-visible:border-amber/60 focus-visible:outline-none"
                  />
                  <Button variant="ghost" size="sm" className="px-1.5" icon={<Check size={15} />}
                    onClick={handleRename} title="Save" />
                  <Button variant="ghost" size="sm" className="px-1.5" icon={<X size={15} />}
                    onClick={() => setTitleDraft(null)} title="Cancel" />
                </>
              ) : (
                <>
                  <span className="flex-1 truncate font-mono text-sm text-ink">
                    {detail?.title ?? "…"}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="px-1.5"
                    icon={<Pencil size={13} />}
                    onClick={() => setTitleDraft(detail?.title ?? "")}
                    disabled={!detail}
                    title="Rename session"
                  />
                  <Button
                    variant={activityOpen ? "outline" : "ghost"}
                    size="sm"
                    className="gap-1.5 px-2"
                    icon={turnRunning ? <Loader2 size={13} className="animate-spin" /> : <Activity size={13} />}
                    onClick={() => setActivityOpen((o) => !o)}
                    title="Toggle activity log"
                  >
                    <span className="text-[11px]">{events.length > 0 ? events.length : "Log"}</span>
                  </Button>
                </>
              )}
            </div>

            <div
              ref={streamRef}
              className="flex-1 space-y-3 overflow-y-auto p-4"
            >
              {turns.length === 0 && !pendingMessage && (
                <div className="text-sm text-muted">
                  Describe what you want to build — e.g. “spin up a landing page”.
                </div>
              )}
              {turns.map((t) => (
                <div key={t.id} className="space-y-1">
                  <div className="rounded-lg border border-edge bg-base px-3 py-2 text-sm text-ink">
                    {t.prompt}
                  </div>
                  <div className="flex items-center gap-2 px-1">
                    <Badge tone={TURN_TONE[t.status]}>{t.status}</Badge>
                    {t.provider && (
                      <span className="font-mono text-[11px] text-muted">{t.provider}</span>
                    )}
                  </div>
                  {t.response && (
                    <div
                      className="markdown px-1 text-sm text-ink/80"
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(t.response) }}
                    />
                  )}
                  {t.error && <div className="px-1 text-sm text-bad">{t.error}</div>}
                </div>
              ))}

              {/* Optimistic in-flight turn: the user's message shows immediately. */}
              {pendingMessage && (
                <div className="space-y-1">
                  <div className="rounded-lg border border-edge bg-base px-3 py-2 text-sm text-ink">
                    {pendingMessage}
                  </div>
                  <GeneratingIndicator
                    latest={curatedEvents[curatedEvents.length - 1]?.message ?? undefined}
                  />
                </div>
              )}

              {sendError && (
                <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
                  {sendError}
                </div>
              )}

              {/* Detailed activity log — off by default (toggled from the header). */}
              {activityOpen && (events.length > 0 || streamingText) && (
                <div className="space-y-1 rounded-lg border border-edge bg-base/60 p-3 font-mono text-xs">
                  {shownEvents.map((ev, i) => (
                    <div key={i} className="flex gap-2">
                      <span className={`w-16 shrink-0 ${KIND_COLOR[ev.kind] || "text-muted"}`}>
                        {ev.kind}
                      </span>
                      <span className="flex-1 break-words text-ink/90">
                        {ev.message || ev.phase || ""}
                      </span>
                    </div>
                  ))}
                  {streamingText && (
                    <div className="whitespace-pre-wrap text-ink/70">
                      {streamingText.slice(-800)}
                      <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse-live bg-live align-middle" />
                    </div>
                  )}
                  {hiddenCount > 0 && (
                    <button
                      onClick={() => setRawOpen((o) => !o)}
                      className="flex items-center gap-1 pt-1 text-[11px] text-muted hover:text-ink"
                    >
                      <ChevronRight
                        size={12}
                        className={`transition-transform ${rawOpen ? "rotate-90" : ""}`}
                      />
                      {rawOpen ? "hide raw log" : `show raw log (${hiddenCount} internal)`}
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Composer */}
            <div className="space-y-2 border-t border-edge p-3">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-muted">agent</span>
                <Select
                  value={providerSwitch}
                  onChange={(e) => setProviderSwitch(e.target.value)}
                  className="h-8 text-xs"
                >
                  <option value="">
                    {detail?.provider ? `current (${detail.provider})` : "default"}
                  </option>
                  {providerIds.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex items-end gap-2">
                <textarea
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                      e.preventDefault();
                      handleSend();
                    }
                  }}
                  rows={2}
                  placeholder="Describe the next change…  (⌘/Ctrl+Enter to send)"
                  className="flex-1 resize-none rounded-md border border-edge bg-base px-3 py-2 text-sm text-ink placeholder:text-muted focus-visible:border-amber/60 focus-visible:outline-none"
                />
                <Button
                  variant="primary"
                  icon={<Send size={14} />}
                  onClick={handleSend}
                  disabled={!message.trim() || sending}
                >
                  Send
                </Button>
              </div>
            </div>
          </>
        )}
      </Card>

      {/* ── Live preview ─────────────────────────────────────────────────── */}
      <Card className="flex h-[70vh] flex-col p-0">
        <div className="flex items-center justify-between border-b border-edge px-3 py-2">
          <div className="flex items-center gap-2">
            <StatusDot tone={turnRunning ? "live" : "ok"} pulse={turnRunning} />
            <span className="font-mono text-xs uppercase tracking-widest text-muted">Preview</span>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="px-1.5"
            icon={<RefreshCw size={13} />}
            onClick={() => setPreviewNonce((n) => n + 1)}
            disabled={!previewSrc}
            title="Refresh preview"
          />
        </div>
        {previewSrc ? (
          <iframe
            key={previewSrc}
            src={previewSrc}
            title="Session live preview"
            className="flex-1 w-full bg-white"
            sandbox="allow-scripts allow-same-origin"
          />
        ) : (
          <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted">
            {selectedId ? "Nothing built yet — send a turn to generate a page." : "No session selected."}
          </div>
        )}
      </Card>
    </div>
  );
}
