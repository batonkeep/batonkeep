// SessionView.tsx — the build-session work surface (M1.2). Left: session list +
// new-session form. Center: chat input with a provider switcher + turn history
// with the live event stream. Right: the live preview <iframe> pointed at the
// session's token-authenticated workspace, refreshed when a turn completes.
// D-track: composed from ui/ primitives (Button, Badge, Card, StatusDot, Select).
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/common";
import "highlight.js/styles/github-dark.css";
import { Activity, Archive, Check, ChevronDown, ChevronLeft, ChevronRight, Cloud, Copy, Download, FileCode, Folder, FolderKanban, Globe, History, Link2, Loader2, Lock, Paperclip, Pencil, Plus, RefreshCw, RotateCcw, Search, Send, Shield, Square, SquareTerminal, Trash2, X } from "lucide-react";
import type { CloudflareStatus, ContextSource, ExecPolicy, FileChange, FileEntry, ImageModel, Project, ProviderCatalog, ProviderHealth, Publish, Session, SessionTemplate, SessionTurn, Version, WorkItem } from "../types";
import { api } from "../api";
import { useSessionEvents, type SessionEvent } from "../useLiveFeed";
import { fmtTime } from "../format";
import { Badge, Button, Card, Field, Input, Modal, Select, StatusDot, Tabs, type Tone } from "../ui";

function renderMarkdown(src: string): string {
  // Sanitize: turn responses/file content contain model- and web-sourced text
  // that ends up in dangerouslySetInnerHTML. marked does NOT sanitize, so run
  // its HTML through DOMPurify to strip <script>/on* and other injection vectors.
  return DOMPurify.sanitize(marked.parse(src, { async: false }) as string);
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
      // Label with the user-facing extension, not hljs's grammar name (HTML
      // highlights via the "xml" grammar — tagging index.html "XML" reads wrong).
      return { html: hljs.highlight(content, { language: lang }).value, lang: ext };
    }
    const auto = hljs.highlightAuto(content);
    return { html: auto.value, lang: auto.language || "text" };
  } catch {
    // Defensive: never let highlighting break the viewer — show escaped plain text.
    const esc = content.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return { html: esc, lang: "text" };
  }
}

// How a workspace file should be rendered in the Preview pane (D-0028). Images
// render in an <img>, markdown through the markdown renderer, text/code in a
// syntax-highlighted block, and anything else (binary, unknown) offers a download.
type FileKind = "image" | "markdown" | "code" | "binary";
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "webp", "gif", "svg", "avif", "ico", "bmp"]);
const CODE_EXTS = new Set([
  "txt", "js", "jsx", "ts", "tsx", "py", "css", "scss", "json", "yaml", "yml",
  "toml", "ini", "cfg", "sh", "bash", "html", "htm", "xml", "sql", "csv", "rb",
  "go", "rs", "java", "c", "h", "cpp", "env", "gitignore", "log", "conf",
]);
function fileKind(path: string): FileKind {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  if (IMAGE_EXTS.has(ext)) return "image";
  if (ext === "md" || ext === "markdown") return "markdown";
  if (CODE_EXTS.has(ext) || path.split("/").pop()?.startsWith(".")) return "code";
  return "binary";
}

// Default a Cloudflare Pages project name from a session title (mirrors the backend).
function slugProject(title: string): string {
  const s = (title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 58);
  return s || "batonkeep-site";
}

// A file the user opened from a chat link, shown in the Preview pane.
interface OpenFile {
  path: string;
  kind: FileKind;
  content: string;
  loading: boolean;
  error?: string;
}

// The live feed shows a curated set of meaningful events by default. `log` frames
// carry internal scaffolding (the assembled turn-context prompt, CLI launch flags
// like --dangerously-skip-permissions, raw end-of-stream markers) that would read
// as unsafe/noisy to the user — those are only shown when "raw" is expanded.
const CURATED_KINDS = new Set(["phase", "tool", "subagent", "result", "route", "error", "approval"]);
function isCurated(ev: SessionEvent): boolean {
  return CURATED_KINDS.has(ev.kind);
}

// Animated "generating…" line shown while a turn is in flight. Surfaces the most
// recent step so the user sees forward progress, not a frozen spinner.
function GeneratingIndicator({ latest }: { latest?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 text-sm text-live">
      <Loader2 size={14} className="shrink-0 animate-spin" />
      <span className="shrink-0">Generating…</span>
      {latest && <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted">{latest}</span>}
    </div>
  );
}

// Small hover-revealed copy button — copies `text` to the clipboard with a brief
// check-mark confirmation. Used on chat messages (prompt + agent response).
function CopyButton({ text, title, className = "" }: { text: string; title: string; className?: string }) {
  const [done, setDone] = useState(false);
  if (!text) return null;
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          /* clipboard unavailable (e.g. non-secure context) — no-op */
        }
      }}
      title={title}
      aria-label={title}
      className={`rounded p-0.5 text-muted hover:bg-edge/40 hover:text-ink ${className}`}
    >
      {done ? <Check size={12} className="text-ok" /> : <Copy size={12} />}
    </button>
  );
}

// D-0017 thread 2: the turn *result* is the workspace files it produced — the
// "capture the artifacts" reframe. Renders the changed files as the headline,
// each clickable to open in the viewer; agent prose is demoted to a caption.
const STATUS_DOT: Record<string, string> = {
  added: "text-ok",
  changed: "text-brand",
  removed: "text-bad",
};

function FileChangeRow({ f, onOpen }: { f: FileChange; onOpen: (path: string) => void }) {
  // Within a folder group the leading dir is implied — show just the basename.
  const label = f.path.includes("/") ? f.path.slice(f.path.lastIndexOf("/") + 1) : f.path;
  return (
    <button
      onClick={() => onOpen(f.path)}
      className="group flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-edge/40"
      title={`Open ${f.path}`}
    >
      <FileCode size={13} className={`shrink-0 ${STATUS_DOT[f.status] || "text-muted"}`} />
      <span className="flex-1 truncate font-mono text-xs text-ink group-hover:underline">
        {label}
      </span>
      <span className="shrink-0 font-mono text-[11px]">
        {f.additions != null && <span className="text-ok">+{f.additions}</span>}
        {f.additions != null && f.deletions != null && " "}
        {f.deletions != null && <span className="text-bad">−{f.deletions}</span>}
        {f.additions == null && f.deletions == null && <span className="text-muted">bin</span>}
      </span>
    </button>
  );
}

// A collapsible folder group of changed files, collapsed by default so a package
// install or build step doesn't flood the artifact card (D-0029 part 3).
function FolderGroup({
  dir,
  files,
  onOpen,
}: {
  dir: string;
  files: FileChange[];
  onOpen: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-edge/40"
        title={`${open ? "Collapse" : "Expand"} ${dir}/`}
      >
        {open ? <ChevronDown size={13} className="shrink-0 text-muted" /> : <ChevronRight size={13} className="shrink-0 text-muted" />}
        <Folder size={13} className="shrink-0 text-muted" />
        <span className="flex-1 truncate font-mono text-xs text-ink">{dir}/</span>
        <span className="shrink-0 font-mono text-[11px] text-muted">{files.length}</span>
      </button>
      {open && (
        <ul className="ml-4 space-y-0.5 border-l border-edge/60 pl-1">
          {files.map((f) => (
            <li key={f.path}>
              <FileChangeRow f={f} onOpen={onOpen} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function ArtifactList({
  files,
  onOpen,
}: {
  files: FileChange[];
  onOpen: (path: string) => void;
}) {
  // Group by top-level folder; root files stay inline, folders collapse (D-0029).
  const { roots, folders } = useMemo(() => {
    const roots: FileChange[] = [];
    const folders = new Map<string, FileChange[]>();
    for (const f of files) {
      const slash = f.path.indexOf("/");
      if (slash === -1) {
        roots.push(f);
      } else {
        const dir = f.path.slice(0, slash);
        (folders.get(dir) ?? folders.set(dir, []).get(dir)!).push(f);
      }
    }
    return { roots, folders: [...folders.entries()].sort((a, b) => a[0].localeCompare(b[0])) };
  }, [files]);

  if (files.length === 0) return null;
  return (
    <div className="rounded-lg border border-edge bg-base/60 px-2 py-1.5">
      <div className="px-1 pb-1 text-[11px] font-medium text-muted">
        {files.length} {files.length === 1 ? "file" : "files"} ·{" "}
        <span className="text-muted/80">result</span>
      </div>
      <ul className="space-y-0.5">
        {folders.map(([dir, group]) => (
          <FolderGroup key={dir} dir={dir} files={group} onOpen={onOpen} />
        ))}
        {roots.map((f) => (
          <li key={f.path}>
            <FileChangeRow f={f} onOpen={onOpen} />
          </li>
        ))}
      </ul>
    </div>
  );
}

// Compact human file size for the Files browser (P-0034).
function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function FileEntryRow({
  entry,
  onOpen,
  active,
  stripDir,
}: {
  entry: FileEntry;
  onOpen: (path: string) => void;
  active: boolean;
  stripDir: boolean;
}) {
  const label = stripDir && entry.path.includes("/")
    ? entry.path.slice(entry.path.lastIndexOf("/") + 1)
    : entry.path;
  return (
    <button
      onClick={() => onOpen(entry.path)}
      title={`Open ${entry.path}`}
      className={`group flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-edge/40 ${active ? "bg-brand/10" : ""}`}
    >
      <FileCode size={13} className={`shrink-0 ${active ? "text-brand" : "text-muted"}`} />
      <span className="flex-1 truncate font-mono text-xs text-ink group-hover:underline">{label}</span>
      <span className="shrink-0 font-mono text-[10px] text-muted">{fmtSize(entry.size)}</span>
    </button>
  );
}

// Persistent workspace file browser — the Files tab (P-0034). Lists every file in
// the session workspace, grouped by top-level folder with folders collapsed by
// default (consistent with the artifact card, D-0029), each click-to-open in the
// right Preview pane via the same viewFile path as the artifact list (D-0028).
// A folder node in the workspace file tree. `dirs` are nested subfolders keyed by
// their own (last-segment) name; `files` are the entries that live directly in
// this folder. Built recursively so arbitrarily deep trees (e.g. dist/assets/…)
// render as real nested folders, not a flattened single level.
interface FileNode {
  dirs: Map<string, FileNode>;
  files: FileEntry[];
}

function buildFileTree(entries: FileEntry[]): FileNode {
  const root: FileNode = { dirs: new Map(), files: [] };
  for (const e of entries) {
    const parts = e.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      let child = node.dirs.get(seg);
      if (!child) {
        child = { dirs: new Map(), files: [] };
        node.dirs.set(seg, child);
      }
      node = child;
    }
    node.files.push(e);
  }
  return root;
}

// Total files at or below a node — used for the folder's count badge.
function countFiles(node: FileNode): number {
  let n = node.files.length;
  for (const child of node.dirs.values()) n += countFiles(child);
  return n;
}

function FileTreeChildren({
  node,
  prefix,
  onOpen,
  activePath,
}: {
  node: FileNode;
  prefix: string; // path prefix of this node ("" at root, "dist/" one level down)
  onOpen: (path: string) => void;
  activePath?: string;
}) {
  const dirs = [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const files = [...node.files].sort((a, b) => a.path.localeCompare(b.path));
  return (
    <>
      {dirs.map(([name, child]) => (
        <FileTreeFolder
          key={prefix + name + "/"}
          name={name}
          path={prefix + name}
          node={child}
          onOpen={onOpen}
          activePath={activePath}
        />
      ))}
      {files.map((e) => (
        <li key={e.path}>
          <FileEntryRow entry={e} onOpen={onOpen} active={e.path === activePath} stripDir />
        </li>
      ))}
    </>
  );
}

function FileTreeFolder({
  name,
  path,
  node,
  onOpen,
  activePath,
}: {
  name: string;
  path: string; // full path of this folder, no trailing slash
  node: FileNode;
  onOpen: (path: string) => void;
  activePath?: string;
}) {
  // Auto-expand if the currently-open file lives anywhere under this folder.
  const [open, setOpen] = useState(() => !!activePath && activePath.startsWith(path + "/"));
  return (
    <li>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-edge/40"
        title={`${open ? "Collapse" : "Expand"} ${path}/`}
      >
        {open ? <ChevronDown size={13} className="shrink-0 text-muted" /> : <ChevronRight size={13} className="shrink-0 text-muted" />}
        <Folder size={13} className="shrink-0 text-muted" />
        <span className="flex-1 truncate font-mono text-xs text-ink">{name}/</span>
        <span className="shrink-0 font-mono text-[11px] text-muted">{countFiles(node)}</span>
      </button>
      {open && (
        <ul className="ml-4 space-y-0.5 border-l border-edge/60 pl-1">
          <FileTreeChildren node={node} prefix={path + "/"} onOpen={onOpen} activePath={activePath} />
        </ul>
      )}
    </li>
  );
}

function FileBrowser({
  entries,
  loading,
  onOpen,
  activePath,
}: {
  entries: FileEntry[];
  loading: boolean;
  onOpen: (path: string) => void;
  activePath?: string;
}) {
  const tree = useMemo(() => buildFileTree(entries), [entries]);

  return (
    <div className="flex-1 overflow-y-auto p-3">
      {loading && entries.length === 0 ? (
        <div className="flex items-center justify-center py-10 text-sm text-muted">
          <Loader2 size={14} className="mr-2 animate-spin" /> Loading files…
        </div>
      ) : entries.length === 0 ? (
        <div className="px-2 py-10 text-center text-sm text-muted">
          No files in this workspace yet — run a turn to generate some.
        </div>
      ) : (
        <ul className="space-y-0.5">
          <FileTreeChildren node={tree} prefix="" onOpen={onOpen} activePath={activePath} />
        </ul>
      )}
    </div>
  );
}

// Lazy so xterm.js only loads when a web-TTY session is actually opened.
const WebTtyConsole = lazy(() => import("./WebTtyConsole"));

interface Props {
  sessions: Session[];
  // S0 substrate: selectable Projects for new sessions (default preselected).
  projects: Project[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onSessionsChanged: () => void; // reload the session list in the parent
  providers: ProviderHealth[];
  consoleAvailable: boolean; // web console enabled (gates the web-TTY launcher)
  consoleToken: string; // token presented to /ws/tty
  // When app-auth is on the session is the unlock gate; the legacy console token
  // is not required (mirrors the same logic in ProvidersPanel / D-0023).
  appAuthEnabled: boolean;
}

const TURN_TONE: Record<SessionTurn["status"], Tone> = {
  running: "live",
  succeeded: "ok",
  failed: "bad",
  cancelled: "neutral",  // P-0057/D-0051: user-interrupted turn
};

const KIND_COLOR: Record<string, string> = {
  log: "text-muted",
  phase: "text-ink",
  tool: "text-brand",
  subagent: "text-brand",
  result: "text-ok",
  error: "text-bad",
  route: "text-live",
  approval: "text-amber-400",
};

/** P-0046 slice 3b: a code-exec approval awaiting the operator's decision,
 * derived from the session event stream (a request with no matching resolution). */
export interface PendingApproval {
  requestId: string;
  code: string;
  label: string | null;
}

function derivePendingApproval(events: SessionEvent[]): PendingApproval | null {
  const resolved = new Set<string>();
  for (const ev of events) {
    if (ev.kind === "approval" && ev.data?.resolved) resolved.add(ev.data.request_id);
  }
  // Latest unresolved request wins.
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (
      ev.kind === "approval" &&
      ev.data?.request_id &&
      !ev.data?.resolved &&
      !resolved.has(ev.data.request_id)
    ) {
      return { requestId: ev.data.request_id, code: ev.data.code ?? "", label: ev.data.label ?? null };
    }
  }
  return null;
}

export default function SessionView({
  sessions,
  projects,
  selectedId,
  onSelect,
  onSessionsChanged,
  providers,
  consoleAvailable,
  consoleToken,
  appAuthEnabled,
}: Props) {
  const [mode, setMode] = useState<"chat" | "terminal" | "files">("chat"); // center-pane lane
  const [files, setFiles] = useState<FileEntry[]>([]); // workspace files (Files tab)
  const [filesLoading, setFilesLoading] = useState(false);
  // S0.5: workspace → evidence package capture (Files-tab header action).
  const [pkgBusy, setPkgBusy] = useState(false);
  const [pkgMsg, setPkgMsg] = useState<string | null>(null);
  const [pkgModal, setPkgModal] = useState(false);
  const [pkgItems, setPkgItems] = useState<WorkItem[]>([]);
  const [pkgPin, setPkgPin] = useState(""); // work-item id to pin to ("" = none)
  const [detail, setDetail] = useState<Session | null>(null);
  const [turns, setTurns] = useState<SessionTurn[]>([]);
  const [message, setMessage] = useState("");
  // S0.4: the selected session's declared context sources back the composer's
  // `@` typeahead (inserted as the projected `context/<rel_path>` the agent sees).
  const [ctxSources, setCtxSources] = useState<ContextSource[]>([]);
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null);
  const [mentionIdx, setMentionIdx] = useState(0);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  // S0.4: explicit expand/collapse toggles per project group in the session list.
  // Unset = the default policy (only the selected session's group is open).
  const [groupToggles, setGroupToggles] = useState<Record<string, boolean>>({});
  const [providerSwitch, setProviderSwitch] = useState("");
  // P-0049: per-session model override for the active API provider ("" = the
  // provider's catalog default). Backed by the provider's catalog of enabled models.
  const [modelSwitch, setModelSwitch] = useState("");
  const [sessionCatalog, setSessionCatalog] = useState<ProviderCatalog | null>(null);
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [creating, setCreating] = useState(false);
  const [confidentialDraft, setConfidentialDraft] = useState(false); // new-session local-only pin
  // S0 substrate: Project for the next new session ("" = the owner default).
  const [projectDraft, setProjectDraft] = useState("");
  // S0.4: optional WorkItem link for the next new session ("" = none). Linking is
  // what puts WORKITEM.md in the agent's workspace; fixed at creation like the project.
  const [workItemDraft, setWorkItemDraft] = useState("");
  const [draftItems, setDraftItems] = useState<WorkItem[]>([]);
  const [previewNonce, setPreviewNonce] = useState(0);
  const [rawOpen, setRawOpen] = useState(false);
  // Activity log defaults open: long agentic turns need continuous feedback, not
  // a buried toggle. The toggle still works; the choice sticks for this browser.
  const [activityOpen, setActivityOpen] = useState(
    () => localStorage.getItem("bk.activityOpen") !== "0",
  );
  const toggleActivity = useCallback(() => {
    setActivityOpen((o) => {
      localStorage.setItem("bk.activityOpen", o ? "0" : "1");
      return !o;
    });
  }, []);
  const [titleDraft, setTitleDraft] = useState<string | null>(null); // non-null = editing
  const [pendingMessage, setPendingMessage] = useState<string | null>(null); // optimistic turn
  const [sendError, setSendError] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [versions, setVersions] = useState<Version[]>([]);
  const [diffFor, setDiffFor] = useState<string | null>(null); // commit whose diff is shown
  const [diffText, setDiffText] = useState<string>("");
  const [restoring, setRestoring] = useState<string | null>(null); // commit being restored
  const [capturing, setCapturing] = useState(false); // terminal-lane artifact capture in flight
  const [summary, setSummary] = useState<string | null>(null); // ledger cross-provider memory (D-0017 thread 1)
  const [summarizing, setSummarizing] = useState(false);
  const [publish, setPublish] = useState<Publish | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploaded, setUploaded] = useState<string[]>([]); // paths dropped this session, for chips
  const [sessionQuery, setSessionQuery] = useState(""); // filter the session list
  const [templates, setTemplates] = useState<SessionTemplate[]>([]); // task-type starters
  const [imageModels, setImageModels] = useState<ImageModel[]>([]); // P-0046 slice 6: image-gen catalog
  const [activeTemplate, setActiveTemplate] = useState<string | null>(null); // template used to create the current session
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
  // Carries the template id from handleCreate across the selectedId-reset useEffect.
  // undefined = no pending create (normal session switch → reset to null).
  const pendingTemplateRef = useRef<string | null | undefined>(undefined);
  const [importing, setImporting] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [gitUrl, setGitUrl] = useState("");
  const [gitBranch, setGitBranch] = useState("");
  const [importError, setImportError] = useState<string | null>(null);
  const [isMobile, setIsMobile] = useState(() => typeof window !== "undefined" && window.innerWidth < 1024);
  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 1024);
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  // Distinct provider instance ids for the switcher. Suspended providers
  // (operator enabled=false) are skipped in routing, so they must not be
  // selectable here either — exclude them from the switcher list.
  const providerIds = useMemo(
    () =>
      Array.from(
        new Set(providers.filter((p) => p.enabled !== false).map((p) => p.name))
      ),
    [providers]
  );
  // Which instances are CLI-backed → can be driven as a live terminal (the `>_`
  // marker + the Terminal-mode gate). API/mock can only Chat.
  const providerKind = useMemo(() => {
    const m: Record<string, string> = {};
    providers.forEach((p) => { m[p.name] = p.kind; });
    return m;
  }, [providers]);
  // The instance the composer/terminal acts as: explicit switch → session's →
  // first available.
  const activeInstance = providerSwitch || detail?.provider || providerIds[0] || "";
  const terminalCapable = providerKind[activeInstance] === "cli";
  // The code-exec execution policy only governs the API path (ModelExecutor's
  // tool loop). CLI providers run their own binary with their own permission
  // model and never touch our tool registry, so the selector is irrelevant for
  // them (P-0046). Show it only for API-path provider kinds.
  const activeKind = providerKind[activeInstance];
  const execPolicyRelevant =
    activeKind != null && activeKind !== "cli" && activeKind !== "mock";

  // P-0049: load the active API provider's catalog so the composer can pick a model.
  // Keyed by template (== instance id for built-in API providers); 404 (custom/CLI)
  // → no picker, falls back to the provider default.
  useEffect(() => {
    if (!execPolicyRelevant || !activeInstance) { setSessionCatalog(null); return; }
    let live = true;
    api.getProviderCatalog(activeInstance)
      .then((c) => { if (live) setSessionCatalog(c); })
      .catch(() => { if (live) setSessionCatalog(null); });
    return () => { live = false; };
  }, [activeInstance, execPolicyRelevant]);

  // Reflect the session's persisted model override in the picker on load/switch.
  useEffect(() => { setModelSwitch(detail?.model ?? ""); }, [detail?.model, activeInstance]);
  // Terminal mode needs the web console unlocked (it spawns a CLI) + a CLI provider.
  // With app-auth the session is the unlock gate; no separate token is required.
  const canConsole = consoleAvailable && (appAuthEnabled || consoleToken.trim().length > 0);
  const terminalReady = canConsole && !!selectedId && terminalCapable;

  // Never strand the user in Terminal mode when it stops being available (provider
  // switched to API/mock, session closed, console locked).
  useEffect(() => {
    if (mode === "terminal" && !terminalReady) setMode("chat");
  }, [mode, terminalReady]);

  const loadTurns = useCallback(() => {
    if (!selectedId) return;
    api.listTurns(selectedId).then(setTurns).catch(() => { });
  }, [selectedId]);

  const loadVersions = useCallback(() => {
    if (!selectedId) return;
    api.listVersions(selectedId).then(setVersions).catch(() => { });
  }, [selectedId]);

  const loadPublish = useCallback(() => {
    if (!selectedId) return;
    api.getPublish(selectedId).then(setPublish).catch(() => { });
  }, [selectedId]);

  // S0.4: project name lookup (badge + list grouping) and context sources for
  // the `@` selector, reloaded when the selection moves to another project.
  const projectName = useCallback(
    (id: string | null | undefined) => projects.find((p) => p.id === id)?.name ?? null,
    [projects],
  );
  const selectedProjectId = sessions.find((s) => s.id === selectedId)?.project_id ?? null;
  useEffect(() => {
    setMention(null);
    if (!selectedProjectId) {
      setCtxSources([]);
      return;
    }
    api.listContextSources(selectedProjectId).then(setCtxSources).catch(() => setCtxSources([]));
  }, [selectedProjectId]);

  // Workspace file listing for the Files tab (P-0034). Always-current: refreshed
  // when the tab opens and whenever a turn/capture changes the workspace.
  const loadFiles = useCallback(() => {
    if (!selectedId) return;
    setFilesLoading(true);
    api.listFiles(selectedId)
      .then(setFiles)
      .catch(() => setFiles([]))
      .finally(() => setFilesLoading(false));
  }, [selectedId]);

  // S0.5: snapshot the workspace at HEAD into the project's evidence store
  // (zip + MANIFEST.json), optionally handing it to a work item in the same
  // call (the pin materializes into that work item's future workspaces).
  // Idempotent per commit — the backend returns the existing rows when
  // nothing changed; 409 surfaces as the message.
  const openPkgModal = useCallback(() => {
    if (!selectedId) return;
    setPkgPin("");
    setPkgItems([]);
    setPkgModal(true);
    const pid = detail?.project_id;
    if (pid) {
      api
        .listWorkItems(pid)
        .then((items) =>
          setPkgItems(items.filter((w) => !["done", "dropped"].includes(w.state))),
        )
        .catch(() => setPkgItems([]));
    }
  }, [selectedId, detail?.project_id]);

  const capturePackage = useCallback(() => {
    if (!selectedId || pkgBusy) return;
    setPkgBusy(true);
    setPkgMsg(null);
    api.packageWorkspace(selectedId, pkgPin ? Number(pkgPin) : null)
      .then((res) =>
        setPkgMsg(
          (res.existing
            ? "Already captured for this version"
            : `Captured as evidence #${res.package.id}`) +
            (pkgPin ? " · pinned" : ""),
        ),
      )
      .catch((err: Error) => setPkgMsg(err.message))
      .finally(() => {
        setPkgBusy(false);
        setPkgModal(false);
      });
  }, [selectedId, pkgBusy, pkgPin]);

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
    setSummary(null);
    setDiffFor(null);
    setDiffText("");
    setPublish(null);
    setCopied(false);
    setUploaded([]);
    setMessage("");
    setMobilePane("chat");
    setMode("chat");
    setOpenFile(null);
    setFiles([]);
    // Consume the pending template set by handleCreate, or reset to null on a
    // normal session switch where no create was in flight.
    setActiveTemplate(pendingTemplateRef.current !== undefined ? pendingTemplateRef.current : null);
    pendingTemplateRef.current = undefined;
    setCfUrl(null);
    setCfError(null);
    if (!selectedId) return;
    api.getSession(selectedId).then(setDetail).catch(() => { });
    loadTurns();
    loadPublish();
  }, [selectedId, loadTurns, loadPublish]);

  // When the WS confirms the turn is live ('running'), load it into the turn
  // list so it appears in DB-driven views. pendingMessage stays visible — it is
  // the user message bubble + GeneratingIndicator and must not be cleared until
  // the completed turn is in hand (clearing it here was the regression that made
  // the message and animation disappear before the page was refreshed).
  // When the turn finishes, refresh turns + history + preview and clear the
  // optimistic placeholder now that the real completed card is available.
  useEffect(() => {
    if (!lastTurn) return;
    if (lastTurn.status === "running") {
      // Fetch the running turn record so the turn list reflects it.
      loadTurns();
    } else {
      // Turn completed (succeeded or failed).
      loadTurns();
      loadVersions();
      // Refresh the session detail so the live cost counter reflects this turn's spend.
      if (selectedId) api.getSession(selectedId).then(setDetail).catch(() => { });
      onSessionsChanged(); // session.updated_at + provider may have changed
      setPreviewNonce((n) => n + 1);
      if (mode === "files") loadFiles(); // keep the Files tab current after a turn
      setPendingMessage(null); // clear now that the real completed card is available
    }
  }, [lastTurn, loadTurns, loadVersions, onSessionsChanged, mode, loadFiles, selectedId]);

  // Load the workspace file listing whenever the Files tab opens (P-0034).
  useEffect(() => {
    if (mode === "files") loadFiles();
  }, [mode, loadFiles]);

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
    api.listSessionTemplates().then(setTemplates).catch(() => { });
    api.listImageModels().then(setImageModels).catch(() => { });
  }, []);

  // S0.4: the effective Project for the new-session form (the select shows the
  // owner default when nothing is picked), and its open work items for the
  // optional work-item link. Reloaded when the picked project changes.
  const draftProjectId = projectDraft || (projects.find((p) => p.is_default)?.id ?? "");
  useEffect(() => {
    setWorkItemDraft("");
    if (selectedId || !draftProjectId) {
      setDraftItems([]);
      return;
    }
    let cancelled = false;
    api
      .listWorkItems(draftProjectId)
      .then((items) => {
        if (!cancelled) setDraftItems(items.filter((w) => w.state !== "done" && w.state !== "dropped"));
      })
      .catch(() => {
        if (!cancelled) setDraftItems([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, draftProjectId]);

  const handleCreate = async (template?: string) => {
    setCreating(true);
    // Write the template intent before onSelect triggers the selectedId useEffect.
    pendingTemplateRef.current = template ?? null;
    try {
      const s = await api.createSession({
        ...(template ? { template } : { title: "Untitled session" }),
        confidential: confidentialDraft,
        // "" = let the backend resolve the owner's default project.
        project_id: projectDraft || null,
        work_item_id: workItemDraft ? Number(workItemDraft) : null,
      });
      onSessionsChanged();
      onSelect(s.id);
    } catch {
      // If creation fails, clear the pending ref so we don't poison the next switch.
      pendingTemplateRef.current = undefined;
    } finally {
      setCreating(false);
    }
  };

  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null); // type-to-confirm modal
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  const performDelete = async (s: Session) => {
    setDeletingId(s.id);
    try {
      await api.deleteSession(s.id);
      if (selectedId === s.id) onSelect(null); // deselect if we just removed the open one
      onSessionsChanged();
      setDeleteTarget(null);
      setDeleteConfirmText("");
    } catch {
      // leave the row in place; the list reload on next change will reconcile
    } finally {
      setDeletingId(null);
    }
  };

  const handleDelete = (s: Session) => {
    // Sessions with content (turns) or a live publish get a stronger type-to-confirm
    // guard; empty/Untitled sessions stay a single quick confirm (clearing clutter).
    const hasContent = (s.turn_count ?? 0) > 0 || !!s.published;
    if (hasContent) {
      setDeleteConfirmText("");
      setDeleteTarget(s);
      return;
    }
    const label = s.title?.trim() || "this session";
    if (window.confirm(`Delete "${label}"? This removes its workspace and cannot be undone.`)) {
      performDelete(s);
    }
  };

  const handleToggleConfidential = async () => {
    if (!selectedId || !detail) return;
    const updated = await api.updateSession(selectedId, { confidential: !detail.confidential });
    setDetail(updated);
    onSessionsChanged();
  };

  // P-0046 slice 3b: change the session's code-exec execution policy.
  const handleSetExecPolicy = async (policy: ExecPolicy) => {
    if (!selectedId || !detail || policy === detail.exec_policy) return;
    const updated = await api.updateSession(selectedId, { exec_policy: policy });
    setDetail(updated);
  };

  // P-0046 slice 6: change the session's image-gen model ("" → provider default).
  const handleSetImageModel = async (value: string) => {
    if (!selectedId || !detail) return;
    const next = value || null;
    if (next === (detail.image_model_id ?? null)) return;
    const updated = await api.updateSession(selectedId, { image_model_id: value });
    setDetail(updated);
  };

  // Set / raise / clear the per-session spend cap. Prompt for a USD figure;
  // empty or 0 clears the cap (no session budget). The cap is enforced
  // cumulatively across turns, stopping at the next step that would exceed it.
  const handleSetBudget = async () => {
    if (!selectedId || !detail) return;
    const current = detail.budget_usd != null ? String(detail.budget_usd) : "";
    const raw = window.prompt(
      "Session budget in USD (blank or 0 = no cap). Enforced cumulatively; " +
      "a turn stops at the next step that would exceed it.",
      current,
    );
    if (raw === null) return; // cancelled
    const value = Number.parseFloat(raw.trim());
    const budget_usd = Number.isFinite(value) && value > 0 ? value : 0;
    const updated = await api.updateSession(selectedId, { budget_usd });
    setDetail(updated);
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

  // ── S0.4: `@` context selector ────────────────────────────────────────────
  // Typing `@` opens a typeahead over the projected context sources and the
  // workspace files; selecting inserts the plain relative path the agent can
  // resolve in its workspace (provider-neutral — it's just text in the prompt).
  const mentionItems = useMemo(() => {
    if (!mention) return [];
    const q = mention.query.toLowerCase();
    const ctx = ctxSources.map((s) => ({
      insert: `context/${s.rel_path}${s.kind === "dir" ? "/" : ""}`,
      label: `context/${s.rel_path}`,
      hint: s.domain ? `${s.kind} · ${s.domain}` : s.kind,
    }));
    // Identity is the full inserted path — same-named files in different dirs
    // stay distinct entries, while a declared source and its projected workspace
    // copy (identical path) collapse to the source entry.
    const seen = new Set(ctx.map((c) => c.label));
    const ws = files
      .filter((f) => !seen.has(f.path))
      .map((f) => ({ insert: f.path, label: f.path, hint: "workspace" }));
    return [...ctx, ...ws].filter((c) => c.label.toLowerCase().includes(q)).slice(0, 8);
  }, [mention, ctxSources, files]);

  const detectMention = (value: string, cursor: number) => {
    const m = /(^|\s)@([\w./~-]*)$/.exec(value.slice(0, cursor));
    if (!m) {
      setMention(null);
      return;
    }
    if (files.length === 0 && !filesLoading) loadFiles();
    setMention({ start: cursor - m[2].length - 1, query: m[2] });
    setMentionIdx(0);
  };

  const applyMention = (item: { insert: string }) => {
    if (!mention) return;
    const el = composerRef.current;
    const cursor = el?.selectionStart ?? message.length;
    const next = message.slice(0, mention.start) + item.insert + " " + message.slice(cursor);
    setMessage(next);
    setMention(null);
    requestAnimationFrame(() => {
      if (el) {
        const pos = mention.start + item.insert.length + 1;
        el.focus();
        el.setSelectionRange(pos, pos);
      }
    });
  };

  const handleSend = async () => {
    const text = message.trim();
    if (!selectedId || !text || sending) return;
    // Optimistic: show the user's message and clear the input immediately.
    setMessage("");
    setSendError(null);
    setPendingMessage(text);
    setSending(true);
    try {
      // POST returns 202 immediately once the turn record is created and the agent
      // is dispatched as a background task. The turn's live progress streams over
      // the WS; turnRunning stays true via lastTurn?.status === 'running' until the
      // WS broadcasts 'succeeded'/'failed'. No gateway timeout possible.
      await api.createTurn(selectedId, {
        message: text,
        provider: providerSwitch || undefined,
        // P-0049: pin the model for this and subsequent turns on the API path. Only
        // send when a catalog is in play (API provider); "" clears to the default.
        model: sessionCatalog ? modelSwitch : undefined,
      });
      onSessionsChanged(); // session.provider/model may have switched
    } catch (err) {
      // Only genuine pre-dispatch errors (session not found, no provider selected)
      // reach here. Restore the message so the user can retry.
      setSendError(err instanceof Error ? err.message : "Failed to send message");
      setMessage(text);
      setPendingMessage(null);
    } finally {
      // Clear the HTTP-level 'sending' flag. The WS-driven turnRunning keeps the
      // "generating…" indicator alive until the agent finishes.
      setSending(false);
    }
  };

  // P-0057/D-0051: interrupt the in-flight turn (best-effort). The backend stops the
  // agent and marks the turn "cancelled"; the WS broadcast flips turnRunning off.
  const handleCancel = async () => {
    if (!selectedId || currentTurnId == null || !turnRunning || cancelling) return;
    setCancelling(true);
    setSendError(null);
    try {
      await api.cancelTurn(selectedId, currentTurnId);
      setPendingMessage(null);
      loadTurns();  // resync persisted status (cancelled) after a switch-back interrupt
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Could not stop the agent");
    } finally {
      setCancelling(false);
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
      const kind = fileKind(path);
      setMobilePane("preview"); // surface it on mobile, where panes are tabbed
      // Images and binaries render straight from the raw-file URL (an <img> or a
      // download button) — no point fetching their bytes as text (D-0028).
      if (kind === "image" || kind === "binary") {
        setOpenFile({ path, kind, content: "", loading: false });
        return;
      }
      setOpenFile({ path, kind, content: "", loading: true });
      try {
        const content = await api.getFileContent(selectedId, path);
        setOpenFile({ path, kind, content, loading: false });
      } catch (e) {
        setOpenFile({ path, kind, content: "", loading: false, error: e instanceof Error ? e.message : "Could not load file" });
      }
    },
    [selectedId]
  );

  // Capture the web-TTY terminal lane's workspace edits as a version + artifact
  // turn (D-0017 thread 2). The terminal CLI edits the workspace with no engine
  // commit boundary, so we snapshot on demand (Capture button) and on stop. The
  // captured turn surfaces in the transcript with the same artifact card.
  const captureTerminal = useCallback(async (): Promise<boolean> => {
    if (!selectedId || capturing) return false;
    setCapturing(true);
    try {
      const turn = await api.captureTerminal(selectedId, activeInstance);
      if (turn) {
        loadTurns();
        loadVersions();
        onSessionsChanged();
        setPreviewNonce((n) => n + 1);
      }
      return !!turn;
    } catch (e) {
      setSendError(e instanceof Error ? e.message : "Could not capture terminal output");
      return false;
    } finally {
      setCapturing(false);
    }
  }, [selectedId, activeInstance, capturing, loadTurns, loadVersions, onSessionsChanged]);

  // Cross-provider memory (D-0017 thread 1): load the ledger summary when History
  // opens, and let the user refresh it on demand.
  const loadSummary = useCallback(() => {
    if (!selectedId) return;
    api.getSummary(selectedId).then((s) => setSummary(s.summary)).catch(() => { });
  }, [selectedId]);

  const refreshSummary = useCallback(async () => {
    if (!selectedId || summarizing) return;
    setSummarizing(true);
    try {
      const s = await api.refreshSummary(selectedId);
      setSummary(s.summary);
    } catch (e) {
      setSendError(e instanceof Error ? e.message : "Could not refresh memory");
    } finally {
      setSummarizing(false);
    }
  }, [selectedId, summarizing]);

  useEffect(() => {
    if (historyOpen) loadSummary();
  }, [historyOpen, loadSummary]);

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
      ? `${api.previewUrl(detail.id, detail.preview_token)}${previewNonce ? `?_=${previewNonce}` : ""
      }`
      : null;

  // Prefer the live turn.update, but on a fresh session mount lastTurn is null until
  // the next WS frame — so switching away from a session with a running turn and back
  // would otherwise drop the in-flight state (and revert Stop→Send). Fall back to the
  // latest persisted turn (listTurns, ordered by seq) so the running state + Stop
  // button survive a session switch (P-0057/D-0051).
  const latestPersistedTurn = turns.length ? turns[turns.length - 1] : null;
  const currentTurnId = lastTurn?.id ?? latestPersistedTurn?.id ?? null;
  const currentTurnStatus = lastTurn?.status ?? latestPersistedTurn?.status ?? null;
  const turnRunning = currentTurnStatus === "running" || sending;
  const curatedEvents = useMemo(() => events.filter(isCurated), [events]);
  const hiddenCount = events.length - curatedEvents.length;
  const shownEvents = rawOpen ? events : curatedEvents;

  // What to show beside "Generating…" so the user sees live progress without
  // opening the Activity panel. Prefer the agent's streaming narration (its last
  // non-empty line — the step it's on right now), falling back to the latest
  // curated step (tool/phase) when the lane emits structured events instead of prose.
  const latestActivity = useMemo(() => {
    const line = streamingText.split("\n").map((s) => s.trim()).filter(Boolean).pop();
    return line || curatedEvents[curatedEvents.length - 1]?.message || undefined;
  }, [streamingText, curatedEvents]);

  // P-0046 slice 3b: a pending code-exec approval (confirmation policy).
  const pendingApproval = useMemo(() => derivePendingApproval(events), [events]);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const resolveApproval = useCallback(
    async (approved: boolean) => {
      if (!selectedId || !pendingApproval || approvalBusy) return;
      setApprovalBusy(true);
      try {
        await api.resolveApproval(selectedId, pendingApproval.requestId, approved);
      } catch {
        /* the turn's await will time out → denied, so a failed POST is non-fatal */
      } finally {
        setApprovalBusy(false);
      }
    },
    [selectedId, pendingApproval, approvalBusy],
  );

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
        {sessions.length > 0 && (
          <div className="relative">
            <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
            <Input
              value={sessionQuery}
              onChange={(e) => setSessionQuery(e.target.value)}
              placeholder="Search sessions…"
              className="!h-9 pl-8 text-xs"
              aria-label="Search sessions"
            />
          </div>
        )}
        {(() => {
          const q = sessionQuery.trim().toLowerCase();
          const visible = q
            ? sessions.filter(
              (s) =>
                s.title.toLowerCase().includes(q) || (s.provider ?? "").toLowerCase().includes(q),
            )
            : sessions;
          if (sessions.length > 0 && visible.length === 0) {
            return (
              <div className="rounded-lg border border-dashed border-edge p-3 text-center text-xs text-muted">
                No sessions match.
              </div>
            );
          }
          const card = (s: Session) => {
          const active = s.id === selectedId;
          return (
            <div
              key={s.id}
              className={`group relative rounded-lg border transition-colors ${active ? "border-brand/50 bg-brand/10" : "border-edge bg-panel/60 hover:border-brand/30"
                }`}
            >
              <button
                onClick={() => onSelect(s.id)}
                className="block w-full rounded-lg px-3 py-2 pr-9 text-left"
              >
                <span className="block truncate font-mono text-sm text-ink">{s.title}</span>
                <span className="font-mono text-[11px] text-muted">
                  {s.provider ?? "—"} · {fmtTime(s.updated_at)}
                </span>
              </button>
              <button
                onClick={() => handleDelete(s)}
                disabled={deletingId === s.id}
                title="Delete session"
                aria-label={`Delete session ${s.title}`}
                className="absolute right-1.5 top-1.5 rounded p-1.5 text-muted opacity-0 transition-opacity hover:bg-bad/10 hover:text-bad focus-visible:opacity-100 group-hover:opacity-100 disabled:opacity-50"
              >
                {deletingId === s.id ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
              </button>
            </div>
          );
          };
          // S0.4: sessions spanning more than one project group under project
          // headers (first-seen order — the list is already recency-sorted).
          // A single-project list stays flat: no header noise for the common case.
          // Groups are collapsed by default; only the selected session's group
          // opens on its own. An active search expands everything (matches must
          // never hide), and explicit toggles win otherwise.
          const groups: { pid: string; items: Session[] }[] = [];
          for (const s of visible) {
            const pid = s.project_id ?? "";
            const g = groups.find((x) => x.pid === pid);
            if (g) g.items.push(s);
            else groups.push({ pid, items: [s] });
          }
          if (groups.length <= 1) return visible.map(card);
          return groups.map((g) => {
            const open = q
              ? true
              : groupToggles[g.pid] ?? g.items.some((s) => s.id === selectedId);
            return (
              <div key={g.pid || "none"} className="space-y-2">
                <button
                  onClick={() => setGroupToggles((t) => ({ ...t, [g.pid]: !open }))}
                  className="flex w-full items-center gap-1.5 rounded px-1 pt-1 font-mono text-[10px] uppercase tracking-wider text-muted hover:text-ink"
                  aria-expanded={open}
                >
                  {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                  <FolderKanban size={11} /> {projectName(g.pid) ?? "No project"}
                  <span className="ml-auto rounded border border-edge bg-base px-1.5 text-[10px] normal-case">
                    {g.items.length}
                  </span>
                </button>
                {open && g.items.map(card)}
              </div>
            );
          });
        })()}
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
            {/* Hero headline (D-0027 item 2) */}
            <div className="text-center">
              <p className="font-mono text-base font-semibold text-ink">What do you want to build?</p>
              <p className="mt-1 text-xs text-muted">Describe it — your AI plan writes the code, installs dependencies, and runs it.</p>
            </div>
            <div className="grid w-full max-w-2xl grid-cols-2 gap-3 sm:grid-cols-3">
              {/* Default session — hero card */}
              <button
                onClick={() => handleCreate()}
                disabled={creating}
                className="col-span-2 flex flex-col gap-2 rounded-xl border-2 border-brand/40 bg-gradient-to-br from-brand/5 to-transparent p-5 text-left transition-all hover:border-brand/70 hover:shadow-md sm:col-span-1 disabled:opacity-50"
              >
                <span className="flex items-center gap-2 font-mono text-sm font-semibold text-brand">
                  <Globe size={16} /> Build &amp; publish
                </span>
                <span className="text-xs text-muted">
                  Describe a site or app — AI writes the code, installs dependencies, and publishes it live.
                </span>
              </button>
              {templates.map((t) => (
                <button
                  key={t.id}
                  onClick={() => handleCreate(t.id)}
                  disabled={creating}
                  className="flex flex-col gap-1.5 rounded-xl border border-edge bg-base p-4 text-left transition-all hover:border-brand/50 hover:shadow-sm disabled:opacity-50"
                >
                  <span className="flex items-center gap-2 font-mono text-sm text-ink">
                    {t.id === "summarize" ? <Activity size={14} /> : t.id === "research" ? <Search size={14} /> : <Pencil size={14} />} {t.label}
                  </span>
                  <span className="text-xs text-muted">{t.description}</span>
                </button>
              ))}
            </div>
            {/* S0 substrate: which Project the new session lands in (default preselected),
                and (S0.4) an optional WorkItem link — what puts WORKITEM.md in the agent's
                workspace. Both are fixed once the session is created. */}
            {projects.length > 0 && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <label className="flex items-center gap-2 text-xs text-muted">
                  <span className="font-mono text-[11px] uppercase tracking-wider">Project</span>
                  <select
                    value={draftProjectId}
                    onChange={(e) => setProjectDraft(e.target.value)}
                    className="rounded-md border border-edge bg-base px-2 py-1 font-mono text-xs text-ink outline-none focus:border-brand/60"
                    aria-label="Project for the new session"
                  >
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}{p.is_default ? " (default)" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                {draftItems.length > 0 && (
                  <label className="flex items-center gap-2 text-xs text-muted">
                    <span className="font-mono text-[11px] uppercase tracking-wider">Work item</span>
                    <select
                      value={workItemDraft}
                      onChange={(e) => setWorkItemDraft(e.target.value)}
                      className="max-w-64 rounded-md border border-edge bg-base px-2 py-1 font-mono text-xs text-ink outline-none focus:border-brand/60"
                      aria-label="Work item for the new session (optional)"
                    >
                      <option value="">none</option>
                      {draftItems.map((w) => (
                        <option key={w.id} value={String(w.id)}>
                          #{w.id} {w.title.length > 48 ? `${w.title.slice(0, 48)}…` : w.title} · {w.state}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
            )}
            <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={confidentialDraft}
                onChange={(e) => setConfidentialDraft(e.target.checked)}
                className="accent-brand"
              />
              <Lock size={12} />
              <span>Keep on this machine (local model only — prompt &amp; files never leave)</span>
            </label>
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
                  <button
                    type="button"
                    onClick={() => detail && setTitleDraft(detail.title ?? "")}
                    disabled={!detail}
                    title="Click to rename session"
                    className="group flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-edge/40"
                  >
                    <span className="truncate font-mono text-sm text-ink">
                      {detail?.title ?? "…"}
                    </span>
                    <Pencil size={12} className="shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100" />
                  </button>
                  {/* S0.4: which project this session belongs to (fixed at creation). */}
                  {detail && projectName(detail.project_id) && (
                    <span
                      className="hidden items-center gap-1 rounded border border-edge bg-base px-1.5 py-0.5 font-mono text-[10px] text-muted sm:flex"
                      title="Project (fixed when the session was created)"
                    >
                      <FolderKanban size={11} /> {projectName(detail.project_id)}
                    </span>
                  )}
                  {detail?.confidential && (
                    <span
                      className="flex items-center gap-1 rounded border border-brand/40 bg-brand/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-brand"
                      title="Confidential — pinned to a local model; nothing leaves this box"
                    >
                      <Lock size={11} /> Local-only
                    </span>
                  )}
                  <Button
                    variant={detail?.confidential ? "outline" : "ghost"}
                    size="sm"
                    className="px-1.5"
                    icon={detail?.confidential ? <Lock size={13} /> : <Shield size={13} />}
                    onClick={handleToggleConfidential}
                    disabled={!detail}
                    title={detail?.confidential ? "Confidential: on — click to allow remote models" : "Make confidential — pin to a local model"}
                  />
                  {detail && execPolicyRelevant && (
                    <Select
                      value={detail.exec_policy}
                      onChange={(e) => handleSetExecPolicy(e.target.value as ExecPolicy)}
                      className="h-8 w-auto max-w-24 py-0 text-[11px] sm:max-w-none"
                      title="Code execution policy — when the agent may run code (P-0046)"
                    >
                      <option value="off">Code: off</option>
                      <option value="confirmation">Code: confirm each</option>
                      <option value="allow-safe">Code: allow safe</option>
                      <option value="auto">Code: auto</option>
                    </Select>
                  )}
                  {detail && execPolicyRelevant && imageModels.length > 0 && (
                    <Select
                      value={detail.image_model_id ?? ""}
                      onChange={(e) => handleSetImageModel(e.target.value)}
                      className="h-8 w-auto max-w-24 py-0 text-[11px] sm:max-w-none"
                      title="Image model — which model the agent uses to generate images. Default follows the provider; you can pick any connected model, including cross-provider (P-0046)."
                    >
                      <option value="">Image: default</option>
                      {imageModels.map((m) => (
                        <option key={m.id} value={m.id} disabled={!m.available}>
                          {`Image: ${m.label}${m.available ? "" : " (no key)"}`}
                        </option>
                      ))}
                    </Select>
                  )}
                  <Button
                    variant={historyOpen ? "outline" : "ghost"}
                    size="sm"
                    className="gap-1.5 px-2"
                    icon={<History size={13} />}
                    onClick={toggleHistory}
                    title="Undo / History — previous versions of this build"
                  >
                    <span className="hidden text-[11px] sm:inline">History</span>
                  </Button>
                  <Button
                    variant={activityOpen ? "outline" : "ghost"}
                    size="sm"
                    className="gap-1.5 px-2"
                    icon={turnRunning ? <Loader2 size={13} className="animate-spin" /> : <Activity size={13} />}
                    onClick={toggleActivity}
                    title="Toggle activity log"
                  >
                    <span className="text-[11px]">{events.length > 0 ? events.length : "Log"}</span>
                  </Button>
                </>
              )}
            </div>

            {/* Chat | Terminal | Files — ways to work the same workspace, one at a
                time. Terminal swaps the transcript+composer for a live CLI you
                drive yourself (web-TTY; CLI providers only). Files is a persistent
                workspace browser (P-0034) so you can re-open any file without
                scrolling history. Shown whenever a session is open. */}
            {inSession && (
              <div className="flex items-center gap-2 border-b border-edge px-4 py-1.5">
                <div className="inline-flex rounded-md border border-edge p-0.5">
                  <button
                    type="button"
                    onClick={() => setMode("chat")}
                    className={`rounded px-2 py-0.5 font-mono text-[11px] ${mode === "chat" ? "bg-brand/15 text-brand" : "text-muted hover:text-ink"}`}
                  >
                    Chat
                  </button>
                  {consoleAvailable && (
                    <button
                      type="button"
                      onClick={() => terminalReady && setMode("terminal")}
                      disabled={!terminalReady}
                      title={terminalReady
                        ? "Drive this provider's CLI live in the session workspace"
                        : terminalCapable
                          ? (appAuthEnabled
                              ? "Web console is not enabled on this deployment"
                              : "Unlock the web console (token in Settings → AI Plans) to use Terminal")
                          : "Terminal needs a CLI provider (›_) — switch the agent below"}
                      className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-mono text-[11px] disabled:opacity-40 ${mode === "terminal" ? "bg-brand/15 text-brand" : "text-muted hover:text-ink"}`}
                    >
                      <SquareTerminal size={11} /> Terminal
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setMode("files")}
                    title="Browse every file in this session's workspace"
                    className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-mono text-[11px] ${mode === "files" ? "bg-brand/15 text-brand" : "text-muted hover:text-ink"}`}
                  >
                    <Folder size={11} /> Files
                  </button>
                </div>
                {mode === "terminal" && (
                  <span className="font-mono text-[11px] text-muted">{activeInstance} · live · you drive every turn</span>
                )}
                {/* Right side: mode-specific action + the live session cost / budget chip. */}
                <div className="ml-auto flex items-center gap-2">
                  {mode === "files" && (
                    <button
                      type="button"
                      onClick={() => loadFiles()}
                      disabled={filesLoading}
                      title="Refresh the file listing"
                      className="inline-flex items-center gap-1 rounded border border-edge px-2 py-0.5 font-mono text-[11px] text-muted hover:text-ink disabled:opacity-40"
                    >
                      {filesLoading ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
                      Refresh
                    </button>
                  )}
                  {mode === "terminal" && (
                    // Capture the session's workspace edits as an artifact turn
                    // (D-0017 thread 2). Also runs automatically on stop.
                    <button
                      type="button"
                      onClick={() => void captureTerminal()}
                      disabled={capturing}
                      title="Capture the files this terminal session changed as a result"
                      className="inline-flex items-center gap-1 rounded border border-edge px-2 py-0.5 font-mono text-[11px] text-muted hover:text-ink disabled:opacity-40"
                    >
                      {capturing ? <Loader2 size={11} className="animate-spin" /> : <FileCode size={11} />}
                      Capture
                    </button>
                  )}
                  {detail && (() => {
                    const spent = detail.cost_usd ?? 0;
                    const cap = detail.budget_usd ?? null;
                    const frac = cap && cap > 0 ? Math.min(spent / cap, 1) : 0;
                    const reached = cap != null && cap > 0 && spent >= cap;
                    return (
                      <button
                        type="button"
                        onClick={handleSetBudget}
                        className={`flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-[11px] ${
                          reached
                            ? "border-bad/50 bg-bad/10 text-bad"
                            : "border-edge text-muted hover:bg-edge/40"
                        }`}
                        title={
                          cap != null
                            ? `Session spend $${spent.toFixed(4)} of $${cap.toFixed(2)} cap. ` +
                              "Enforced cumulatively — a turn stops at the next step that " +
                              "would exceed the cap. Click to raise or clear."
                            : `Session spend $${spent.toFixed(4)}. Click to set a budget cap.`
                        }
                      >
                        <span>${spent.toFixed(spent < 1 ? 4 : 2)}</span>
                        {cap != null && (
                          <>
                            <span className="text-edge">/</span>
                            <span>${cap.toFixed(2)}</span>
                            <span className="relative inline-block h-1 w-8 overflow-hidden rounded bg-edge/60">
                              <span
                                className={`absolute inset-y-0 left-0 ${reached ? "bg-bad" : "bg-brand"}`}
                                style={{ width: `${frac * 100}%` }}
                              />
                            </span>
                          </>
                        )}
                        {reached && <span className="uppercase tracking-wide">raise</span>}
                      </button>
                    );
                  })()}
                </div>
              </div>
            )}

            {mode === "terminal" && terminalReady ? (
              <div className="flex-1 overflow-hidden p-2">
                <Suspense fallback={<div className="p-4 text-xs text-muted">loading terminal…</div>}>
                  <WebTtyConsole
                    key={`${selectedId}:${activeInstance}`}
                    embedded
                    session={selectedId}
                    instance={activeInstance}
                    token={consoleToken}
                    onClose={async () => {
                      // Auto-capture on stop so the session's artifacts are never
                      // lost when the user leaves Terminal mode (founder: button +
                      // auto on stop).
                      await captureTerminal();
                      setMode("chat");
                    }}
                  />
                </Suspense>
              </div>
            ) : mode === "files" ? (
              <div className="flex min-h-0 flex-1 flex-col">
                {/* S0.5: capture the workspace at HEAD as an immutable evidence
                    package (zip + manifest) — the durable artifact a later work
                    item can pin and a cold operator can reproduce from. */}
                <div className="flex items-center justify-between gap-2 border-b border-edge px-3 py-1.5">
                  <span className="shrink-0 font-mono text-[11px] uppercase tracking-widest text-muted">
                    Workspace files
                  </span>
                  <div className="flex min-w-0 items-center gap-2">
                    {pkgMsg && (
                      <span className="min-w-0 truncate text-[11px] text-muted" title={pkgMsg}>
                        {pkgMsg}
                      </span>
                    )}
                    <button
                      onClick={openPkgModal}
                      disabled={pkgBusy}
                      title="Snapshot the workspace at its latest version into the project's evidence (zip + manifest)"
                      className="inline-flex shrink-0 items-center gap-1 rounded-md border border-edge px-2 py-1 text-[11px] text-muted transition hover:text-ink disabled:opacity-50"
                    >
                      {pkgBusy ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <Archive size={12} />
                      )}
                      Capture package
                    </button>
                  </div>
                </div>
                <FileBrowser
                  entries={files}
                  loading={filesLoading}
                  onOpen={viewFile}
                  activePath={openFile?.path}
                />
              </div>
            ) : (
            <>
            <div
              ref={streamRef}
              className="flex-1 space-y-3 overflow-y-auto p-4"
            >
              {/* Undo/History — previous versions of the build, newest first. */}
              {historyOpen && (
                <div className="space-y-1 rounded-lg border border-edge bg-base/60 p-3">
                  {/* Cross-provider memory (D-0017 thread 1): the auto-maintained
                      ledger summary that primes a switched-in agent. */}
                  <div className="mb-2 rounded-md border border-edge/60 bg-panel/40 p-2">
                    <div className="flex items-center gap-1.5 pb-1 font-mono text-[11px] uppercase tracking-widest text-muted">
                      <Activity size={12} /> Memory
                      <button
                        onClick={() => void refreshSummary()}
                        disabled={summarizing}
                        title="Summarize the session so a switched-in agent is primed"
                        className="ml-auto inline-flex items-center gap-1 rounded border border-edge px-1.5 py-0.5 text-[10px] normal-case tracking-normal text-muted hover:text-ink disabled:opacity-40"
                      >
                        {summarizing ? <Loader2 size={10} className="animate-spin" /> : <RefreshCw size={10} />}
                        Refresh
                      </button>
                    </div>
                    {summary ? (
                      <p className="whitespace-pre-wrap text-xs text-ink/80">{summary}</p>
                    ) : (
                      <p className="text-xs text-muted">
                        No summary yet — Refresh to distil the session into portable memory.
                      </p>
                    )}
                  </div>
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
                  {activeTemplate
                    ? (templates.find((t) => t.id === activeTemplate)?.description ?? "Describe what you want to do.")
                    : "Describe what you want to build — e.g. “spin up a landing page”."}
                </div>
              )}
              {turns.map((t) => (
                <div key={t.id} className="space-y-1">
                  <div className="group relative rounded-lg border border-edge bg-base px-3 py-2 text-sm text-ink">
                    {t.prompt}
                    <CopyButton
                      text={t.prompt}
                      title="Copy message"
                      className="absolute right-1.5 top-1.5 opacity-0 transition-opacity group-hover:opacity-100"
                    />
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
                  {/* The result is the artifacts the turn produced (D-0017 thread 2);
                      the agent's prose is demoted to a caption beneath them. */}
                  {t.changed_files && t.changed_files.length > 0 && (
                    <ArtifactList files={t.changed_files} onOpen={viewFile} />
                  )}
                  {t.response && (
                    <div className="group relative">
                      <div
                        className={`markdown px-1 text-sm ${
                          t.changed_files && t.changed_files.length > 0
                            ? "text-muted"
                            : "text-ink/80"
                        }`}
                        onClick={onChatClick}
                        dangerouslySetInnerHTML={{ __html: renderMarkdown(t.response) }}
                      />
                      <CopyButton
                        text={t.response}
                        title="Copy response"
                        className="absolute right-1 top-0 opacity-0 transition-opacity group-hover:opacity-100"
                      />
                    </div>
                  )}
                  {t.error && <div className="px-1 text-sm text-bad">{t.error}</div>}
                </div>
              ))}

              {/* Optimistic in-flight turn: shows the user's message immediately
                  while the 202 response is in-flight. Suppressed once the real
                  turn record is in `turns` (loadTurns fetched it) to avoid a
                  duplicate message bubble while the agent runs. The generating
                  animation is tied to turnRunning independently so it persists. */}
              {pendingMessage && turns[turns.length - 1]?.prompt !== pendingMessage && (
                <div className="rounded-lg border border-edge bg-base px-3 py-2 text-sm text-ink">
                  {pendingMessage}
                </div>
              )}
              {/* Generating indicator: visible whenever a turn is running,
                  regardless of whether pendingMessage is suppressed by the turn
                  appearing in the DB list. */}
              {turnRunning && (
                <GeneratingIndicator latest={latestActivity} />
              )}

              {sendError && (
                <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
                  {sendError}
                </div>
              )}

              {/* P-0046 slice 3b: code-exec approval prompt (confirmation policy).
                  The agent's turn is blocked awaiting this decision. */}
              {pendingApproval && (
                <div className="rounded-lg border border-amber-500/50 bg-amber-500/5 p-3 text-sm">
                  <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-ink">
                    <SquareTerminal size={14} className="shrink-0 text-amber-500" />
                    {pendingApproval.label || "Approve code execution?"}
                  </div>
                  <pre className="mb-2 max-h-48 overflow-auto rounded-md border border-edge/60 bg-base/70 p-2 font-mono text-[11px] leading-relaxed text-ink/90 whitespace-pre-wrap">
                    {pendingApproval.code}
                  </pre>
                  <div className="flex gap-2">
                    <Button
                      variant="primary"
                      size="sm"
                      icon={<Check size={13} />}
                      disabled={approvalBusy}
                      onClick={() => resolveApproval(true)}
                    >
                      Approve & run
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      icon={<X size={13} />}
                      disabled={approvalBusy}
                      onClick={() => resolveApproval(false)}
                    >
                      Deny
                    </Button>
                  </div>
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

            </>
            )}

            {/* Composer — the agent selector + attach/import stay in BOTH modes
                (switch the CLI or drop workspace files while in Terminal); the
                message box + Send are Chat-only, since in Terminal the live CLI
                is the input. */}
            <div className="space-y-2 border-t border-edge p-3">
              {/* Hidden file inputs — always mounted so the buttons can trigger them. */}
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
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-muted">agent</span>
                <Select
                  value={providerSwitch}
                  onChange={(e) => setProviderSwitch(e.target.value)}
                  className="h-7 text-xs"
                >
                  <option value="">
                    {detail?.provider
                      ? `current (${detail.provider})${providerKind[detail.provider] === "cli" ? " ›_" : ""}`
                      : "default"}
                  </option>
                  {providerIds.map((id) => (
                    <option key={id} value={id}>
                      {id}{providerKind[id] === "cli" ? " ›_" : ""}
                    </option>
                  ))}
                </Select>
                {sessionCatalog && (
                  <>
                    <span className="font-mono text-[11px] text-muted">model</span>
                    <Select
                      value={modelSwitch}
                      onChange={(e) => setModelSwitch(e.target.value)}
                      className="h-7 text-xs"
                      title="Model for this API provider (P-0049). Default uses the provider's preferred model."
                    >
                      <option value="">
                        default ({sessionCatalog.preferred.default ?? sessionCatalog.effective_model ?? "—"})
                      </option>
                      {sessionCatalog.models.filter((m) => m.enabled).map((m) => (
                        <option key={m.id} value={m.id}>{m.id}</option>
                      ))}
                    </Select>
                  </>
                )}
                <div className="ml-auto flex items-center gap-0.5">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="px-2 py-1.5"
                    icon={uploading ? <Loader2 size={15} className="animate-spin" /> : <Paperclip size={15} />}
                    onClick={() => fileInputRef.current?.click()}
                    disabled={uploading || sending}
                    title="Attach a file (image, CSV, PDF…) into the workspace"
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    className="px-2 py-1.5"
                    icon={importing ? <Loader2 size={15} className="animate-spin" /> : <Archive size={15} />}
                    onClick={() => { setImportError(null); setImportModalOpen(true); }}
                    disabled={importing || sending}
                    title="Import an existing site (.zip / .tar, or a git URL)"
                  />
                </div>
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
              {mode === "chat" && (
                <div className="relative">
                  {mention && mentionItems.length > 0 && (
                    <div className="absolute bottom-full left-0 z-20 mb-1 w-full max-w-md overflow-hidden rounded-lg border border-edge bg-panel shadow-lg">
                      <p className="border-b border-edge px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
                        Reference context / workspace files
                      </p>
                      {mentionItems.map((item, i) => (
                        <button
                          key={item.label + item.hint}
                          // mousedown, not click: fires before the textarea blur.
                          onMouseDown={(e) => {
                            e.preventDefault();
                            applyMention(item);
                          }}
                          className={`flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left ${i === mentionIdx ? "bg-brand/10" : "hover:bg-edge/40"
                            }`}
                        >
                          <span className="truncate font-mono text-xs text-ink">{item.label}</span>
                          <span className="shrink-0 font-mono text-[10px] text-muted">{item.hint}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  <textarea
                    ref={composerRef}
                    value={message}
                    onChange={(e) => {
                      setMessage(e.target.value);
                      detectMention(e.target.value, e.target.selectionStart ?? e.target.value.length);
                    }}
                    onBlur={() => setMention(null)}
                    onKeyDown={(e) => {
                      if (mention && mentionItems.length > 0) {
                        if (e.key === "ArrowDown") {
                          e.preventDefault();
                          setMentionIdx((i) => (i + 1) % mentionItems.length);
                          return;
                        }
                        if (e.key === "ArrowUp") {
                          e.preventDefault();
                          setMentionIdx((i) => (i - 1 + mentionItems.length) % mentionItems.length);
                          return;
                        }
                        if (e.key === "Tab" || (e.key === "Enter" && !e.metaKey && !e.ctrlKey)) {
                          e.preventDefault();
                          applyMention(mentionItems[mentionIdx]);
                          return;
                        }
                        if (e.key === "Escape") {
                          e.preventDefault();
                          setMention(null);
                          return;
                        }
                      }
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
                    placeholder={isMobile ? "Describe the next change…" : "Describe the next change…  (drop files here · ⌘/Ctrl+Enter to send)"}
                    className="w-full resize-none rounded-md border border-edge bg-base py-2 pl-3 pr-12 text-sm text-ink placeholder:text-muted focus-visible:border-brand/60 focus-visible:outline-none"
                  />
                  {turnRunning ? (
                    /* P-0057/D-0051: interrupt the in-flight turn (best-effort). */
                    <button
                      onClick={handleCancel}
                      disabled={cancelling || currentTurnId == null}
                      title="Stop the agent"
                      className="absolute bottom-1.5 right-1.5 flex h-8 w-8 items-center justify-center rounded text-muted transition-colors hover:text-bad disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      {cancelling ? <Loader2 size={16} className="animate-spin" /> : <Square size={15} className="fill-current" />}
                    </button>
                  ) : (
                    <button
                      onClick={handleSend}
                      disabled={!message.trim() || sending}
                      title="Send (⌘/Ctrl+Enter)"
                      className="absolute bottom-1.5 right-1.5 flex h-8 w-8 items-center justify-center rounded text-muted transition-colors hover:text-brand disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      {sending ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
                    </button>
                  )}
                </div>
              )}
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
                  {/* Icon-only below sm so the action row fits a phone width (D-0055) */}
                  <span className="hidden text-[11px] sm:inline">Download</span>
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
              <span className="hidden text-[11px] sm:inline">Cloudflare</span>
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
              <span className="hidden text-[11px] sm:inline">{publish?.published ? "Update" : "Publish"}</span>
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
            rawHref={selectedId ? api.fileRawUrl(selectedId, openFile.path) : "#"}
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

      {/* S0.5: capture the workspace as an evidence package, optionally handing
          it to a work item (the pin materializes into its future workspaces). */}
      <Modal
        open={pkgModal}
        onClose={() => setPkgModal(false)}
        title="Capture workspace package"
      >
        <div className="space-y-3">
          <p className="text-xs text-muted">
            Snapshots the workspace at its latest committed version into the project's
            evidence: a zip with a <span className="font-mono">MANIFEST.json</span> of
            per-file digests. Idempotent — recapturing an unchanged workspace reuses the
            existing package.
          </p>
          <Field
            label="Pin to work item (optional)"
            hint="Hands the package to that work item: its future sessions receive the zip read-only under context/evidence/."
          >
            <Select value={pkgPin} onChange={(e) => setPkgPin(e.target.value)}>
              <option value="">— no pin —</option>
              {pkgItems.map((w) => (
                <option key={w.id} value={String(w.id)}>
                  #{w.id} · {w.title} ({w.state})
                </option>
              ))}
            </Select>
          </Field>
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setPkgModal(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={capturePackage}
              disabled={pkgBusy}
              icon={pkgBusy ? <Loader2 size={14} className="animate-spin" /> : <Archive size={14} />}
            >
              {pkgBusy ? "Capturing…" : "Capture"}
            </Button>
          </div>
        </div>
      </Modal>

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

      {/* Type-to-confirm delete — for sessions with content or a live publish. */}
      <Modal
        open={deleteTarget !== null}
        onClose={() => { setDeleteTarget(null); setDeleteConfirmText(""); }}
        title="Delete session"
        footer={deleteTarget && (
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => { setDeleteTarget(null); setDeleteConfirmText(""); }}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              className="!bg-bad hover:!bg-bad/90"
              disabled={deleteConfirmText.trim().toLowerCase() !== "delete" || deletingId === deleteTarget.id}
              onClick={() => performDelete(deleteTarget)}
              icon={deletingId === deleteTarget.id ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
            >
              Delete session
            </Button>
          </div>
        )}
      >
        {deleteTarget && (
          <div className="space-y-3 text-sm">
            <p className="text-ink">
              This permanently removes <span className="font-mono font-semibold">{deleteTarget.title}</span> and
              its workspace. This cannot be undone.
            </p>
            <ul className="space-y-1 text-xs text-muted">
              {(deleteTarget.turn_count ?? 0) > 0 && (
                <li>· {deleteTarget.turn_count} turn{deleteTarget.turn_count === 1 ? "" : "s"} of build history will be lost.</li>
              )}
              {deleteTarget.published && (
                <li className="text-bad">· The live published site will be taken offline.</li>
              )}
            </ul>
            <Field label={`Type "delete" to confirm`}>
              <Input
                value={deleteConfirmText}
                onChange={(e) => setDeleteConfirmText(e.target.value)}
                placeholder="delete"
                aria-label="Type delete to confirm"
                autoFocus
              />
            </Field>
          </div>
        )}
      </Modal>
    </div>
  );
}

// In-pane viewer for a workspace file opened from a chat link (P-0016 b):
// syntax-highlighted, with copy + download, and a close back to the live preview.
function FileViewer({
  file,
  rawHref,
  downloadHref,
  copied,
  onCopy,
  onClose,
}: {
  file: OpenFile;
  rawHref: string;
  downloadHref: string;
  copied: boolean;
  onCopy: () => void;
  onClose: () => void;
}) {
  const { html, lang } = useMemo(
    () =>
      file.kind === "code" && file.content
        ? highlightCode(file.content, file.path)
        : { html: "", lang: "" },
    [file.kind, file.content, file.path]
  );
  // Copy only applies to the text lanes (code/markdown carry fetched content).
  const copyable = file.kind === "code" || file.kind === "markdown";
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
          {copyable && (
            <Button
              variant="ghost"
              size="sm"
              className="px-1.5"
              icon={copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
              onClick={onCopy}
              disabled={!file.content}
              title="Copy file content"
            />
          )}
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
      ) : file.kind === "image" ? (
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-base/40 p-4">
          <img
            src={rawHref}
            alt={file.path}
            className="max-h-full max-w-full object-contain"
          />
        </div>
      ) : file.kind === "binary" ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
          <p className="text-sm text-muted">
            This file can’t be previewed here.
          </p>
          <a href={downloadHref} className="inline-flex">
            <Button variant="outline" size="sm" className="gap-1.5" icon={<Download size={13} />}>
              Download file
            </Button>
          </a>
        </div>
      ) : file.kind === "markdown" ? (
        <div
          className="markdown min-h-0 flex-1 overflow-auto p-4 text-sm"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(file.content) }}
        />
      ) : (
        <pre className="hljs min-h-0 flex-1 overflow-auto p-3 text-[12px] leading-relaxed">
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
      )}
    </div>
  );
}
