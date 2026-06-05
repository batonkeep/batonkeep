// SessionView.tsx — the build-session work surface (M1.2). Left: session list +
// new-session form. Center: chat input with a provider switcher + turn history
// with the live event stream. Right: the live preview <iframe> pointed at the
// session's token-authenticated workspace, refreshed when a turn completes.
// D-track: composed from ui/ primitives (Button, Badge, Card, StatusDot, Select).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import { Activity, Check, ChevronRight, Copy, Download, Globe, History, Link2, Loader2, Paperclip, Pencil, Plus, RefreshCw, RotateCcw, Send, X } from "lucide-react";
import type { ProviderHealth, Publish, Session, SessionTemplate, SessionTurn, Version } from "../types";
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
  tool: "text-brand",
  subagent: "text-brand",
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
  const [historyOpen, setHistoryOpen] = useState(false);
  const [versions, setVersions] = useState<Version[]>([]);
  const [diffFor, setDiffFor] = useState<string | null>(null); // commit whose diff is shown
  const [diffText, setDiffText] = useState<string>("");
  const [restoring, setRestoring] = useState<string | null>(null); // commit being restored
  const [publish, setPublish] = useState<Publish | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploaded, setUploaded] = useState<string[]>([]); // paths dropped this session, for chips
  const [templates, setTemplates] = useState<SessionTemplate[]>([]); // task-type starters

  const { events, streamingText, lastTurn } = useSessionEvents(selectedId);
  const streamRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Distinct provider instance ids for the switcher (grouped by what's healthy).
  const providerIds = useMemo(
    () => Array.from(new Set(providers.map((p) => p.name))),
    [providers]
  );

  const loadTurns = useCallback(() => {
    if (!selectedId) return;
    api.listTurns(selectedId).then(setTurns).catch(() => {});
  }, [selectedId]);

  const loadVersions = useCallback(() => {
    if (!selectedId) return;
    api.listVersions(selectedId).then(setVersions).catch(() => {});
  }, [selectedId]);

  const loadPublish = useCallback(() => {
    if (!selectedId) return;
    api.getPublish(selectedId).then(setPublish).catch(() => {});
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
    setHistoryOpen(false);
    setVersions([]);
    setDiffFor(null);
    setDiffText("");
    setPublish(null);
    setCopied(false);
    setUploaded([]);
    setMessage("");
    if (!selectedId) return;
    api.getSession(selectedId).then(setDetail).catch(() => {});
    loadTurns();
    loadPublish();
  }, [selectedId, loadTurns, loadPublish]);

  // When a turn finishes, refresh the turn list + version history and bust the
  // preview iframe so it reflects the latest workspace edits (Cache-Control:
  // no-store on the backend).
  useEffect(() => {
    if (lastTurn && lastTurn.status !== "running") {
      loadTurns();
      loadVersions();
      setPreviewNonce((n) => n + 1);
    }
  }, [lastTurn, loadTurns, loadVersions]);

  // Auto-scroll the chat to the latest content — but not while History is open,
  // since the History card sits at the top of the stream and we want it in view.
  useEffect(() => {
    if (historyOpen) return;
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
  }, [events, streamingText, turns, pendingMessage, historyOpen]);

  // Opening History reveals its card at the top of the stream — scroll up to it.
  useEffect(() => {
    if (historyOpen && streamRef.current) streamRef.current.scrollTop = 0;
  }, [historyOpen]);

  // Task-type starters (P-0010 / D-0011). Loaded once; rendered as cards in the
  // empty state. A blank session is always available via the header + button.
  useEffect(() => {
    api.listSessionTemplates().then(setTemplates).catch(() => {});
  }, []);

  const handleCreate = async (template?: string) => {
    setCreating(true);
    try {
      const s = await api.createSession(template ? { template } : { title: "Untitled session" });
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

  const handleUpload = async (files: FileList | null) => {
    if (!selectedId || !files || files.length === 0 || uploading) return;
    setSendError(null);
    setUploading(true);
    try {
      const res = await api.uploadAssets(selectedId, Array.from(files));
      // Append references so the user can talk about the files by name in the chat.
      const refs = res.paths.join(", ");
      setMessage((m) => (m.trim() ? `${m} ${refs}` : `Use ${refs}`));
      setUploaded((prev) => [...prev, ...res.paths]);
      onSessionsChanged(); // the upload landed as a new version
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Could not upload file(s)");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = ""; // allow re-selecting the same file
    }
  };

  const toggleHistory = () => {
    setHistoryOpen((open) => {
      if (!open) loadVersions();
      return !open;
    });
  };

  // View the diff a version introduced (toggles open/closed). "git" is never named.
  const viewDiff = async (commit: string) => {
    if (diffFor === commit) {
      setDiffFor(null);
      return;
    }
    setDiffFor(commit);
    setDiffText("");
    try {
      const d = await api.versionDiff(selectedId!, commit);
      setDiffText(d.diff || "(no changes)");
    } catch {
      setDiffText("(could not load changes)");
    }
  };

  // Roll back the workspace to an earlier version. The restore lands as a new
  // version (itself undoable); refresh turns, history, and the preview after.
  const handleRestore = async (commit: string) => {
    if (!selectedId || restoring) return;
    setRestoring(commit);
    try {
      await api.restoreVersion(selectedId, commit);
      loadTurns();
      loadVersions();
      setDiffFor(null);
      setPreviewNonce((n) => n + 1);
      onSessionsChanged();
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Could not restore version");
    } finally {
      setRestoring(null);
    }
  };

  // Publish the current build to a public share link, or refresh/revoke it (M1.4).
  const handlePublish = async () => {
    if (!selectedId || publishing) return;
    setPublishing(true);
    try {
      setPublish(await api.publish(selectedId));
      setCopied(false);
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Could not publish");
    } finally {
      setPublishing(false);
    }
  };

  const handleRevoke = async () => {
    if (!selectedId || publishing) return;
    setPublishing(true);
    try {
      setPublish(await api.revokePublish(selectedId));
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Could not revoke");
    } finally {
      setPublishing(false);
    }
  };

  const shareUrl = publish?.share_path ? api.shareUrl(publish.share_path) : null;

  const handleCopyShare = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
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
            onClick={() => handleCreate()}
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
                active ? "border-brand/50 bg-brand/10" : "border-edge bg-panel/60 hover:border-brand/30"
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
          <div className="flex flex-1 flex-col items-center justify-center gap-5 p-6">
            <p className="text-sm text-muted">Start a session</p>
            <div className="grid w-full max-w-2xl gap-3 sm:grid-cols-3">
              {/* Flagship build session (D-0007) + the retention task types (D-0011). */}
              <button
                onClick={() => handleCreate()}
                disabled={creating}
                className="flex flex-col gap-1 rounded-lg border border-edge bg-base p-4 text-left transition-colors hover:border-brand/60 disabled:opacity-50"
              >
                <span className="flex items-center gap-2 font-mono text-sm text-ink">
                  <Globe size={14} /> Build a page
                </span>
                <span className="text-xs text-muted">
                  Describe a site or app in plain English and publish it.
                </span>
              </button>
              {templates.map((t) => (
                <button
                  key={t.id}
                  onClick={() => handleCreate(t.id)}
                  disabled={creating}
                  className="flex flex-col gap-1 rounded-lg border border-edge bg-base p-4 text-left transition-colors hover:border-brand/60 disabled:opacity-50"
                >
                  <span className="flex items-center gap-2 font-mono text-sm text-ink">
                    {t.id === "summarize" ? <Activity size={14} /> : <Pencil size={14} />} {t.label}
                  </span>
                  <span className="text-xs text-muted">{t.description}</span>
                </button>
              ))}
            </div>
            <p className="text-xs text-muted">…or pick an existing session from the list.</p>
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
                    className="flex-1 rounded-md border border-edge bg-base px-2 py-1 font-mono text-sm text-ink focus-visible:border-brand/60 focus-visible:outline-none"
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
                    variant={historyOpen ? "outline" : "ghost"}
                    size="sm"
                    className="gap-1.5 px-2"
                    icon={<History size={13} />}
                    onClick={toggleHistory}
                    title="Undo / History — previous versions of this build"
                  >
                    <span className="text-[11px]">History</span>
                  </Button>
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
              {/* Undo/History — previous versions of the build, newest first. */}
              {historyOpen && (
                <div className="space-y-1 rounded-lg border border-edge bg-base/60 p-3">
                  <div className="flex items-center gap-1.5 pb-1 font-mono text-[11px] uppercase tracking-widest text-muted">
                    <History size={12} /> History
                  </div>
                  {versions.length === 0 && (
                    <div className="text-xs text-muted">No versions yet.</div>
                  )}
                  {versions.map((v, i) => (
                    <div key={v.commit} className="rounded-md border border-edge/60 bg-panel/40 px-2 py-1.5">
                      <div className="flex items-center gap-2">
                        <span className="flex-1 truncate text-xs text-ink">
                          {i === 0 && (
                            <span className="mr-1 font-mono text-[10px] text-ok">current</span>
                          )}
                          {v.message}
                        </span>
                        <span className="font-mono text-[10px] text-muted">{fmtTime(v.ts)}</span>
                        <button
                          onClick={() => viewDiff(v.commit)}
                          className="font-mono text-[10px] text-muted hover:text-ink"
                          title="View what changed"
                        >
                          {diffFor === v.commit ? "hide" : "changes"}
                        </button>
                        {i !== 0 && (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="gap-1 px-1.5 py-0.5"
                            icon={
                              restoring === v.commit ? (
                                <Loader2 size={11} className="animate-spin" />
                              ) : (
                                <RotateCcw size={11} />
                              )
                            }
                            onClick={() => handleRestore(v.commit)}
                            disabled={restoring !== null}
                            title="Restore the build to this version"
                          >
                            <span className="text-[10px]">Restore</span>
                          </Button>
                        )}
                      </div>
                      {diffFor === v.commit && (
                        <pre className="mt-1.5 max-h-56 overflow-auto rounded bg-base p-2 font-mono text-[10px] leading-snug text-ink/80">
                          {diffText || "loading…"}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              )}

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
                    {t.diffstat && (
                      <button
                        onClick={() => {
                          setHistoryOpen(true);
                          loadVersions();
                          if (t.commit_sha) viewDiff(t.commit_sha);
                        }}
                        className="font-mono text-[11px] text-brand hover:underline"
                        title="View this version's changes in History"
                      >
                        {(t.diffstat.split("\n").pop() || "changed").trim()}
                      </button>
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

              {/* Detailed activity log — off by default (toggled from the header).
                  Events are live-only (streamed over WS for the current session),
                  so on an idle session there's nothing until a turn runs. Show an
                  explicit empty state rather than rendering nothing on toggle. */}
              {activityOpen && events.length === 0 && !streamingText && (
                <div className="rounded-lg border border-dashed border-edge bg-base/60 p-3 text-xs text-muted">
                  No live activity yet — send a turn to watch the agent’s steps stream here.
                </div>
              )}
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
              {uploaded.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {uploaded.map((p) => (
                    <span
                      key={p}
                      className="inline-flex items-center gap-1 rounded border border-edge bg-base px-1.5 py-0.5 font-mono text-[10px] text-muted"
                      title="In your workspace — reference it by name"
                    >
                      <Paperclip size={10} /> {p}
                    </span>
                  ))}
                </div>
              )}
              <div className="flex items-end gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept=".png,.jpg,.jpeg,.svg,.webp,.csv,.pdf,.txt,.md,image/*"
                  className="hidden"
                  onChange={(e) => handleUpload(e.target.files)}
                />
                <Button
                  variant="ghost"
                  icon={uploading ? <Loader2 size={14} className="animate-spin" /> : <Paperclip size={14} />}
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading || sending}
                  title="Attach a file (image, CSV, PDF…) — drop into the box too"
                >
                  Attach
                </Button>
                <textarea
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                      e.preventDefault();
                      handleSend();
                    }
                  }}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => {
                    e.preventDefault();
                    handleUpload(e.dataTransfer.files);
                  }}
                  rows={2}
                  placeholder="Describe the next change…  (drop files here · ⌘/Ctrl+Enter to send)"
                  className="flex-1 resize-none rounded-md border border-edge bg-base px-3 py-2 text-sm text-ink placeholder:text-muted focus-visible:border-brand/60 focus-visible:outline-none"
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
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="px-1.5"
              icon={<RefreshCw size={13} />}
              onClick={() => setPreviewNonce((n) => n + 1)}
              disabled={!previewSrc}
              title="Refresh preview"
            />
            {selectedId && (
              <a
                href={api.downloadUrl(selectedId)}
                title="Download a zip of the site’s files"
                className="inline-flex"
              >
                <Button variant="ghost" size="sm" className="gap-1 px-2" icon={<Download size={13} />}>
                  <span className="text-[11px]">Download</span>
                </Button>
              </a>
            )}
            <Button
              variant={publish?.published ? "outline" : "primary"}
              size="sm"
              className="gap-1.5 px-2"
              icon={publishing ? <Loader2 size={13} className="animate-spin" /> : <Globe size={13} />}
              onClick={handlePublish}
              disabled={!selectedId || publishing}
              title={publish?.published ? "Re-publish — refresh the live site to the latest build" : "Publish to a public share link"}
            >
              <span className="text-[11px]">{publish?.published ? "Update" : "Publish"}</span>
            </Button>
          </div>
        </div>

        {/* Share-link bar — shown once published. */}
        {publish?.published && shareUrl && (
          <div className="flex items-center gap-2 border-b border-edge bg-base/60 px-3 py-1.5">
            <Link2 size={13} className="shrink-0 text-ok" />
            <a
              href={shareUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex-1 truncate font-mono text-[11px] text-brand hover:underline"
              title={shareUrl}
            >
              {shareUrl}
            </a>
            <Button
              variant="ghost"
              size="sm"
              className="px-1.5"
              icon={copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
              onClick={handleCopyShare}
              title="Copy share link"
            />
            <Button
              variant="ghost"
              size="sm"
              className="px-1.5 text-bad"
              icon={<X size={13} />}
              onClick={handleRevoke}
              disabled={publishing}
              title="Revoke — take the share link offline (404)"
            />
          </div>
        )}
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
