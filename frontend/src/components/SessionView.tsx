// SessionView.tsx — the build-session work surface (M1.2). Left: session list +
// new-session form. Center: chat input with a provider switcher + turn history
// with the live event stream. Right: the live preview <iframe> pointed at the
// session's token-authenticated workspace, refreshed when a turn completes.
// D-track: composed from ui/ primitives (Button, Badge, Card, StatusDot, Select).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import hljs from "highlight.js/lib/common";
import "highlight.js/styles/github-dark.css";
import { Activity, Archive, Check, ChevronLeft, ChevronRight, Cloud, Copy, Download, FileCode, Globe, History, Link2, Loader2, Paperclip, Pencil, Plus, RefreshCw, RotateCcw, Search, Send, X } from "lucide-react";
import type { CloudflareStatus, ProviderHealth, Publish, Session, SessionTemplate, SessionTurn, Version } from "../types";
import { api } from "../api";
import { useSessionEvents, type SessionEvent } from "../useLiveFeed";
import { fmtTime } from "../format";
import { Badge, Button, Card, Field, Input, Modal, Select, StatusDot, Tabs, type Tone } from "../ui";

function renderMarkdown(src: string): string {
  return marked.parse(src, { async: false }) as string;
}

// Map a filename extension to a highlight.js language; fall back to auto-detect.
const EXT_LANG: Record<string, string> = {
  py: "python", js: "javascript", jsx: "javascript", ts: "typescript",
  tsx: "typescript", json: "json", sh: "bash", bash: "bash", md: "markdown",
  html: "xml", xml: "xml", css: "css", yml: "yaml", yaml: "yaml", sql: "sql",
  toml: "ini", ini: "ini",
};

function highlightCode(content: string, path: string): { html: string; lang: string } {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const lang = EXT_LANG[ext];
  try {
    if (lang && hljs.getLanguage(lang)) {
      return { html: hljs.highlight(content, { language: lang }).value, lang };
    }
    const auto = hljs.highlightAuto(content);
    return { html: auto.value, lang: auto.language || "text" };
  } catch {
    // Defensive: never let highlighting break the viewer — show escaped plain text.
    const esc = content.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return { html: esc, lang: "text" };
  }
}

// Default a Cloudflare Pages project name from a session title (mirrors the backend).
function slugProject(title: string): string {
  const s = (title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 58);
  return s || "batonkeep-site";
}

// A file the user opened from a chat link, shown in the Preview pane.
interface OpenFile {
  path: string;
  content: string;
  loading: boolean;
  error?: string;
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
  // Mobile only: the 3-pane grid can't fit a phone, so a selected session is a
  // master→detail view with a Chat/Preview tab switch. Desktop ignores this.
  const [mobilePane, setMobilePane] = useState<"chat" | "preview">("chat");
  const [openFile, setOpenFile] = useState<OpenFile | null>(null); // file viewed in Preview pane
  const [fileCopied, setFileCopied] = useState(false);
  // Cloudflare Pages connector (D-0009): owner-level config + per-session deploy.
  const [cf, setCf] = useState<CloudflareStatus | null>(null);
  const [cfModalOpen, setCfModalOpen] = useState(false); // credentials setup
  const [cfForm, setCfForm] = useState({ api_token: "", account_id: "" });
  const [cfSaving, setCfSaving] = useState(false);
  const [cfDeployModalOpen, setCfDeployModalOpen] = useState(false); // per-session project + deploy
  const [cfProject, setCfProject] = useState("");
  const [cfDeploying, setCfDeploying] = useState(false);
  const [cfUrl, setCfUrl] = useState<string | null>(null);
  const [cfError, setCfError] = useState<string | null>(null);

  const { events, streamingText, lastTurn } = useSessionEvents(selectedId);
  const streamRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const importInputRef = useRef<HTMLInputElement>(null);
  const [importing, setImporting] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [gitUrl, setGitUrl] = useState("");
  const [gitBranch, setGitBranch] = useState("");
  const [importError, setImportError] = useState<string | null>(null);

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
    setMobilePane("chat");
    setOpenFile(null);
    setCfUrl(null);
    setCfError(null);
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

  // Shared post-import wiring: surface the new site + record the new version.
  const afterImport = (count: number) => {
    setMessage((m) => (m.trim() ? m : `Imported ${count} files — continue from this site.`));
    setPreviewNonce((n) => n + 1); // reflect the imported site in the preview
    onSessionsChanged();           // import landed as a new version
    setImportModalOpen(false);
  };

  // Import an existing site from an archive (zip/tar), preserving structure.
  const handleImport = async (files: FileList | null) => {
    const f = files?.[0];
    if (!selectedId || !f || importing) return;
    setImportError(null);
    setImporting(true);
    try {
      const res = await api.importArchive(selectedId, f);
      afterImport(res.count);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : "Could not import the archive");
    } finally {
      setImporting(false);
      if (importInputRef.current) importInputRef.current.value = "";
    }
  };

  // Import an existing site by cloning a public git URL.
  const handleGitImport = async () => {
    if (!selectedId || !gitUrl.trim() || importing) return;
    setImportError(null);
    setImporting(true);
    try {
      const res = await api.importGit(selectedId, gitUrl.trim(), gitBranch.trim() || undefined);
      setGitUrl("");
      setGitBranch("");
      afterImport(res.count);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : "Could not clone the repository");
    } finally {
      setImporting(false);
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

  // Open a workspace file in the Preview pane (P-0016 b). Fetches the raw content
  // for in-pane syntax-highlighted viewing instead of navigating the browser to it.
  const viewFile = useCallback(
    async (path: string) => {
      if (!selectedId) return;
      setOpenFile({ path, content: "", loading: true });
      setMobilePane("preview"); // surface it on mobile, where panes are tabbed
      try {
        const content = await api.getFileContent(selectedId, path);
        setOpenFile({ path, content, loading: false });
      } catch (e) {
        setOpenFile({ path, content: "", loading: false, error: e instanceof Error ? e.message : "Could not load file" });
      }
    },
    [selectedId]
  );

  // Intercept clicks on the agent's rewritten artifact links
  // (/api/sessions/<id>/files/raw/<path>) and open them in the viewer instead of
  // navigating away. Other links behave normally.
  const onChatClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (!selectedId) return;
      const a = (e.target as HTMLElement).closest("a");
      if (!a) return;
      const href = a.getAttribute("href") || "";
      const marker = `/sessions/${selectedId}/files/raw/`;
      const i = href.indexOf(marker);
      if (i === -1) return;
      e.preventDefault();
      const rel = decodeURIComponent(href.slice(i + marker.length).split(/[?#]/)[0]);
      void viewFile(rel);
    },
    [selectedId, viewFile]
  );

  const handleCopyFile = async () => {
    if (!openFile?.content) return;
    try {
      await navigator.clipboard.writeText(openFile.content);
      setFileCopied(true);
      setTimeout(() => setFileCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  // Cloudflare connector: load owner-level status once on mount.
  useEffect(() => {
    api.getCloudflare().then(setCf).catch(() => setCf({ configured: false }));
  }, []);

  const handleSaveCloudflare = async () => {
    setCfSaving(true);
    setCfError(null);
    try {
      const st = await api.setCloudflare(cfForm);
      setCf(st);
      setCfModalOpen(false);
      setCfForm({ api_token: "", account_id: "" });
      // Credentials in place — proceed to the per-session project + deploy step.
      openDeployModal();
    } catch (e) {
      setCfError(e instanceof Error ? e.message : "Could not save Cloudflare settings");
    } finally {
      setCfSaving(false);
    }
  };

  const handleRemoveCloudflare = async () => {
    try {
      await api.clearCloudflare();
    } catch {
      /* already gone */
    }
    setCf({ configured: false });
    setCfModalOpen(false);
    setCfUrl(null);
  };

  // Open the deploy step with the project prefilled: remembered project → title default.
  const openDeployModal = () => {
    setCfProject(detail?.cf_project || slugProject(detail?.title || ""));
    setCfError(null);
    setCfDeployModalOpen(true);
  };

  // Clicking "Cloudflare": set up credentials first if missing, else go to deploy.
  const handleCloudflareClick = () => {
    if (!selectedId) return;
    if (!cf?.configured) setCfModalOpen(true);
    else openDeployModal();
  };

  // Deploy this session's build to the chosen Cloudflare Pages project.
  const handleDeployCloudflare = async () => {
    if (!selectedId) return;
    setCfDeploying(true);
    setCfError(null);
    setCfUrl(null);
    try {
      const res = await api.deployCloudflare(selectedId, cfProject.trim() || undefined);
      setCfUrl(res.url);
      setCfDeployModalOpen(false);
      if (detail) setDetail({ ...detail, cf_project: res.project }); // remember per-session
    } catch (e) {
      setCfError(e instanceof Error ? e.message : "Deploy failed");
    } finally {
      setCfDeploying(false);
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

  // Mobile master→detail: an open session fills the screen (the App shell has
  // dropped the header chrome + bottom nav), so the pane flexes to fill instead
  // of a fixed 70vh, keeping the composer pinned above the screen bottom. On
  // desktop (lg) all three panes sit in the grid at a fixed height, as before.
  const inSession = !!selectedId;
  // Landing (no session): on mobile, lead with the "Start a session" CTA and
  // demote the session list below it (flex + order), instead of stacking the full
  // list on top of a tall start panel. Desktop keeps the 3-column grid.
  const rootCls = inSession
    ? "flex flex-col gap-2 h-[calc(100dvh-5rem)] lg:grid lg:h-auto lg:grid-cols-[15rem_minmax(0,1fr)_minmax(0,1fr)] lg:gap-4"
    : "flex flex-col gap-4 lg:grid lg:grid-cols-[15rem_minmax(0,1fr)_minmax(0,1fr)] lg:gap-4";
  // On mobile landing the start card sizes to its content (lg:h-[70vh] only on
  // desktop), so it doesn't reserve 70vh of empty space above the session list.
  const paneSize = inSession ? "min-h-0 flex-1 lg:h-[70vh] lg:flex-none" : "lg:h-[70vh]";
  const chatPaneCls = `flex flex-col p-0 ${paneSize} ${inSession && mobilePane === "preview" ? "hidden lg:flex" : ""} ${!inSession ? "order-1 lg:order-none" : ""}`;
  const previewPaneCls = `flex flex-col p-0 ${paneSize} ${!inSession || mobilePane === "chat" ? "hidden lg:flex" : ""}`;

  return (
    <div className={rootCls}>
      {/* ── Session list — on mobile, hidden once a session is selected
            (master→detail); always shown on desktop. ───────────────────── */}
      <div className={`space-y-2 ${selectedId ? "hidden lg:block" : "order-2 lg:order-none"}`}>
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

      {/* ── Mobile detail toolbar: back to the list + Chat/Preview switch.
            Hidden on desktop, where all three panes are visible at once. ── */}
      {selectedId && (
        <div className="flex shrink-0 items-center justify-between gap-2 lg:hidden">
          <Button
            variant="ghost"
            size="sm"
            className="gap-1 px-2"
            icon={<ChevronLeft size={15} />}
            onClick={() => onSelect(null)}
          >
            <span className="text-xs">Sessions</span>
          </Button>
          <Tabs
            tabs={[
              { id: "chat", label: "Chat" },
              { id: "preview", label: "Preview" },
            ] as const}
            active={mobilePane}
            onChange={setMobilePane}
          />
        </div>
      )}

      {/* ── Chat: provider switcher, input, turn history + live events ──── */}
      <Card className={chatPaneCls}>
        {!selectedId ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-5 p-6">
            <p className="text-sm text-muted">Start a session</p>
            <div className="grid w-full max-w-2xl grid-cols-2 gap-3 sm:grid-cols-3">
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
                    {t.id === "summarize" ? <Activity size={14} /> : t.id === "research" ? <Search size={14} /> : <Pencil size={14} />} {t.label}
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
                      onClick={onChatClick}
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
                  className="h-7 text-xs"
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
              <div className="flex items-start gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept=".png,.jpg,.jpeg,.svg,.webp,.csv,.pdf,.txt,.md,image/*"
                  className="hidden"
                  onChange={(e) => handleUpload(e.target.files)}
                />
                <input
                  ref={importInputRef}
                  type="file"
                  accept=".zip,.tar,.gz,.tgz,.bz2,.xz,application/zip,application/x-tar,application/gzip"
                  className="hidden"
                  onChange={(e) => handleImport(e.target.files)}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  className="shrink-0 px-2 py-2"
                  icon={uploading ? <Loader2 size={15} className="animate-spin" /> : <Paperclip size={15} />}
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading || sending}
                  title="Attach a file (image, CSV, PDF…) — drop into the box too"
                />
                <Button
                  variant="ghost"
                  size="sm"
                  className="shrink-0 px-2 py-2"
                  icon={importing ? <Loader2 size={15} className="animate-spin" /> : <Archive size={15} />}
                  onClick={() => { setImportError(null); setImportModalOpen(true); }}
                  disabled={importing || sending}
                  title="Import an existing site (.zip / .tar, or a git URL)"
                />
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
                  size="sm"
                  className="shrink-0"
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

      {/* ── Live preview — on mobile only when a session is open + Preview
            tab is active; always visible on desktop. ─────────────────────── */}
      <Card className={previewPaneCls}>
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
              variant="outline"
              size="sm"
              className="gap-1.5 px-2"
              icon={cfDeploying ? <Loader2 size={13} className="animate-spin" /> : <Cloud size={13} />}
              onClick={handleCloudflareClick}
              disabled={!selectedId || cfDeploying}
              title={cf?.configured
                ? "Deploy this session to Cloudflare Pages"
                : "Set up Cloudflare Pages publishing"}
            >
              <span className="text-[11px]">Cloudflare</span>
            </Button>
            {cf?.configured && (
              <Button
                variant="ghost"
                size="sm"
                className="px-1.5"
                icon={<Pencil size={12} />}
                onClick={() => setCfModalOpen(true)}
                title="Edit Cloudflare credentials"
              />
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

        {/* Cloudflare deploy result / error bar. */}
        {cfUrl && (
          <div className="flex items-center gap-2 border-b border-edge bg-base/60 px-3 py-1.5">
            <Cloud size={13} className="shrink-0 text-ok" />
            <a
              href={cfUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex-1 truncate font-mono text-[11px] text-brand hover:underline"
              title={cfUrl}
            >
              {cfUrl}
            </a>
          </div>
        )}
        {cfError && (
          <div className="border-b border-edge bg-bad/5 px-3 py-1.5 text-[11px] text-bad">
            {cfError}
          </div>
        )}

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
        {openFile ? (
          <FileViewer
            file={openFile}
            downloadHref={selectedId ? api.fileRawUrl(selectedId, openFile.path, true) : "#"}
            copied={fileCopied}
            onCopy={handleCopyFile}
            onClose={() => setOpenFile(null)}
          />
        ) : previewSrc ? (
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

      {/* Import an existing site — archive (zip/tar) or a public git URL. */}
      <Modal
        open={importModalOpen}
        onClose={() => setImportModalOpen(false)}
        title="Import an existing site"
      >
        <div className="space-y-4">
          <p className="text-xs text-muted">
            Bring an existing site into this session, preserving its folder structure. Git
            history isn't carried — the session keeps its own version history.
          </p>

          <div className="space-y-1.5">
            <span className="font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
              From an archive
            </span>
            <Button
              variant="outline"
              size="sm"
              className="w-full"
              icon={importing ? <Loader2 size={14} className="animate-spin" /> : <Archive size={14} />}
              onClick={() => importInputRef.current?.click()}
              disabled={importing}
            >
              Choose a .zip / .tar file…
            </Button>
          </div>

          <div className="flex items-center gap-2 text-[11px] text-muted">
            <span className="h-px flex-1 bg-edge" /> or <span className="h-px flex-1 bg-edge" />
          </div>

          <div className="space-y-2">
            <Field label="From a git URL" hint="Public https repositories only.">
              <Input
                value={gitUrl}
                onChange={(e) => setGitUrl(e.target.value)}
                placeholder="https://github.com/owner/repo.git"
              />
            </Field>
            <Field label="Branch (optional)">
              <Input
                value={gitBranch}
                onChange={(e) => setGitBranch(e.target.value)}
                placeholder="main"
              />
            </Field>
            <Button
              variant="primary"
              size="sm"
              className="w-full"
              icon={importing ? <Loader2 size={14} className="animate-spin" /> : <Globe size={14} />}
              onClick={handleGitImport}
              disabled={importing || !gitUrl.trim()}
            >
              Clone & import
            </Button>
          </div>

          {importError && <p className="text-xs text-bad">{importError}</p>}
        </div>
      </Modal>

      {/* Cloudflare Pages connector setup (D-0009). Token is write-only — the
          backend stores it encrypted and never returns it; the agent never sees it. */}
      <Modal
        open={cfModalOpen}
        onClose={() => setCfModalOpen(false)}
        title="Cloudflare Pages"
        footer={
          <>
            {cf?.configured && (
              <Button variant="ghost" size="sm" className="mr-auto text-bad" onClick={handleRemoveCloudflare}>
                Remove
              </Button>
            )}
            <Button variant="ghost" size="sm" onClick={() => setCfModalOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              size="sm"
              onClick={handleSaveCloudflare}
              disabled={cfSaving || !cfForm.api_token || !cfForm.account_id}
              icon={cfSaving ? <Loader2 size={13} className="animate-spin" /> : undefined}
            >
              Save
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-xs text-muted">
            Connect Cloudflare once for your account. The API token is stored encrypted on the
            backend and used only to publish — it is never exposed to the build agent. You pick
            the Pages project per session when you deploy.
          </p>
          {cf?.configured && (
            <p className="text-[11px] text-muted">
              Connected to account <span className="font-mono text-ink">{cf.account_id}</span>.
              Saving replaces the stored token.
            </p>
          )}
          <Field label="API token" hint="Cloudflare → My Profile → API Tokens (Pages: Edit).">
            <Input
              type="password"
              autoComplete="off"
              value={cfForm.api_token}
              onChange={(e) => setCfForm((f) => ({ ...f, api_token: e.target.value }))}
              placeholder="••••••••••••"
            />
          </Field>
          <Field label="Account ID" hint="Cloudflare dashboard → Workers & Pages → Account ID.">
            <Input
              value={cfForm.account_id}
              onChange={(e) => setCfForm((f) => ({ ...f, account_id: e.target.value }))}
            />
          </Field>
          {cfError && <p className="text-xs text-bad">{cfError}</p>}
        </div>
      </Modal>

      {/* Per-session deploy: choose the Pages project (defaults from the title). */}
      <Modal
        open={cfDeployModalOpen}
        onClose={() => setCfDeployModalOpen(false)}
        title="Deploy to Cloudflare Pages"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setCfDeployModalOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              size="sm"
              onClick={handleDeployCloudflare}
              disabled={cfDeploying || !cfProject.trim()}
              icon={cfDeploying ? <Loader2 size={13} className="animate-spin" /> : <Cloud size={13} />}
            >
              Deploy
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-xs text-muted">
            Each session deploys to its own Cloudflare Pages project. The project is created if it
            doesn't exist; re-deploying updates the same site.
          </p>
          <Field label="Project name" hint="Lowercase letters, digits and hyphens.">
            <Input
              value={cfProject}
              onChange={(e) => setCfProject(e.target.value)}
              placeholder="my-site"
            />
          </Field>
          {cfError && <p className="text-xs text-bad">{cfError}</p>}
        </div>
      </Modal>
    </div>
  );
}

// In-pane viewer for a workspace file opened from a chat link (P-0016 b):
// syntax-highlighted, with copy + download, and a close back to the live preview.
function FileViewer({
  file,
  downloadHref,
  copied,
  onCopy,
  onClose,
}: {
  file: OpenFile;
  downloadHref: string;
  copied: boolean;
  onCopy: () => void;
  onClose: () => void;
}) {
  const { html, lang } = useMemo(
    () => (file.content ? highlightCode(file.content, file.path) : { html: "", lang: "" }),
    [file.content, file.path]
  );
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-edge bg-base/60 px-3 py-1.5">
        <div className="flex min-w-0 items-center gap-2">
          <FileCode size={13} className="shrink-0 text-brand" />
          <span className="truncate font-mono text-[11px] text-ink" title={file.path}>
            {file.path}
          </span>
          {lang && lang !== "text" && (
            <Badge tone="neutral" className="shrink-0 text-[10px] uppercase">{lang}</Badge>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="px-1.5"
            icon={copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
            onClick={onCopy}
            disabled={!file.content}
            title="Copy file content"
          />
          <a href={downloadHref} title="Download this file" className="inline-flex">
            <Button variant="ghost" size="sm" className="px-1.5" icon={<Download size={13} />} />
          </a>
          <Button
            variant="ghost"
            size="sm"
            className="px-1.5"
            icon={<X size={13} />}
            onClick={onClose}
            title="Close — back to live preview"
          />
        </div>
      </div>
      {file.loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted">
          <Loader2 size={14} className="mr-2 animate-spin" /> Loading…
        </div>
      ) : file.error ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-bad">
          {file.error}
        </div>
      ) : (
        <pre className="hljs min-h-0 flex-1 overflow-auto p-3 text-[12px] leading-relaxed">
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
      )}
    </div>
  );
}
