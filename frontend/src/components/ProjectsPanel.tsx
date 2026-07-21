// ProjectsPanel.tsx — S0 substrate UI shell: the Projects list plus a per-project
// detail with Overview / Work / Context / Evidence tabs. Work items carry durable
// intent (objective + next action) across providers; Context lists the declared
// sources with freshness and the pending canonical-write proposals (the human
// approval surface — approving applies the diff, denying records the refusal);
// Evidence is the append-only outcome record (view only, no update path by design).
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  FileText,
  FolderKanban,
  Lock,
  Plus,
  RefreshCw,
} from "lucide-react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api } from "../api";
import type {
  Approval,
  ContextSource,
  Evidence,
  Project,
  ProjectInput,
  WorkItem,
  WorkItemState,
} from "../types";
import { WORK_ITEM_TRANSITIONS } from "../types";
import { Badge, Button, Card, Field, Input, Modal, Select, Tabs, type Tone } from "../ui";
import { fmtTime } from "../format";
import SubtaskChecklist from "./SubtaskChecklist";

interface Props {
  projects: Project[];
  onProjectsChanged: () => void;
}

type Tab = "overview" | "work" | "context" | "evidence";

const WORK_STATE_TONE: Record<WorkItemState, Tone> = {
  open: "neutral",
  in_progress: "live",
  awaiting_approval: "warn",
  blocked: "bad",
  done: "ok",
  dropped: "neutral",
  reopened: "defer",
};

const RISK_TONE: Record<string, Tone> = { low: "neutral", medium: "warn", high: "bad" };

const EVIDENCE_TONE: Record<string, Tone> = {
  report: "brand",
  diff: "defer",
  log: "neutral",
  verification: "ok",
  decision: "warn",
  "asset-ref": "neutral",
};

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Text evidence (reports, diffs, logs) opens in a viewer modal; only unknown/binary
// extensions fall back to the raw link alone.
function evidenceViewKind(relPath: string): "markdown" | "diff" | "text" | null {
  const name = relPath.toLowerCase();
  if (/\.(md|markdown)$/.test(name)) return "markdown";
  if (/\.(diff|patch)$/.test(name)) return "diff";
  if (/\.(txt|log|json|ya?ml|csv)$/.test(name)) return "text";
  return null;
}

// Unified-diff viewer: hunk-colored lines in a scrollable pre.
function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-lg border border-edge bg-base/60 p-3 font-mono text-[11px] leading-relaxed">
      {diff.split("\n").map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---")
          ? "text-muted"
          : line.startsWith("@@")
            ? "text-defer"
            : line.startsWith("+")
              ? "text-ok"
              : line.startsWith("-")
                ? "text-bad"
                : "text-ink";
        return (
          <span key={i} className={`block whitespace-pre ${cls}`}>{line || " "}</span>
        );
      })}
    </pre>
  );
}

export default function ProjectsPanel({ projects, onProjectsChanged }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");

  const selected = useMemo(
    () => projects.find((p) => p.id === selectedId) ?? null,
    [projects, selectedId]
  );

  // Per-project data, loaded on selection and refreshed after mutations.
  const [workItems, setWorkItems] = useState<WorkItem[]>([]);
  const [sources, setSources] = useState<ContextSource[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [error, setError] = useState<string | null>(null);

  // ── Document viewer modal (evidence AND context sources — one viewer) ───────
  // S0.5: context sources get the same modal rendering as evidence; before
  // this, a project's context .md files were not actionable at all.
  type ViewDoc = {
    name: string;
    relPath: string;
    url: string;
    badge?: string;
    badgeTone?: Tone;
    meta?: string;
  };
  const [viewing, setViewing] = useState<ViewDoc | null>(null);
  const [viewText, setViewText] = useState<string | null>(null);

  const openDoc = async (doc: ViewDoc) => {
    setViewing(doc);
    setViewText(null);
    try {
      const res = await fetch(doc.url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setViewText(await res.text());
    } catch (err) {
      setViewText(`Failed to load: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const openEvidence = (e: Evidence) =>
    openDoc({
      name: e.rel_path.split("/").pop() ?? e.rel_path,
      relPath: e.rel_path,
      url: api.evidenceRawUrl(e.id),
      badge: e.kind,
      badgeTone: EVIDENCE_TONE[e.kind] ?? "neutral",
      meta:
        `${e.producer} · ${fmtBytes(e.bytes)} · ${fmtTime(e.created_at)}` +
        (e.digest ? ` · sha256 ${e.digest.slice(0, 12)}` : ""),
    });

  const openSource = (s: ContextSource) =>
    openDoc({
      name: s.rel_path.split("/").pop() ?? s.rel_path,
      relPath: s.rel_path,
      url: api.contextSourceRawUrl(selectedId ?? "", s.id),
      badge: s.kind,
      meta:
        `canonical source · ${s.last_revision ? s.last_revision.slice(0, 12) : "unhashed"}` +
        (s.last_checked_at ? ` · checked ${fmtTime(s.last_checked_at)}` : ""),
    });

  const viewHtml = useMemo(() => {
    if (!viewing || viewText == null) return "";
    if (evidenceViewKind(viewing.relPath) !== "markdown") return "";
    // Evidence may echo model-generated or scraped content; marked does NOT
    // sanitize, so strip injection vectors before dangerouslySetInnerHTML
    // (same rule as run reports).
    return DOMPurify.sanitize(marked.parse(viewText, { async: false }) as string);
  }, [viewing, viewText]);

  // ── Propose-to-canonical modal (S0.5 promotion — the create side) ───────────
  const [proposeFor, setProposeFor] = useState<Evidence | null>(null);
  const [proposeDest, setProposeDest] = useState("");
  const [proposeBusy, setProposeBusy] = useState(false);
  const [proposeErr, setProposeErr] = useState<string | null>(null);

  const openPropose = (e: Evidence) => {
    // Default destination: the stored basename minus its 12-hex capture prefix.
    const base = e.rel_path.split("/").pop() ?? "evidence.md";
    setProposeDest(base.replace(/^[0-9a-f]{12}_/, ""));
    setProposeErr(null);
    setProposeFor(e);
  };

  const submitPropose = () => {
    if (!selectedId || !proposeFor || proposeBusy) return;
    setProposeBusy(true);
    setProposeErr(null);
    api
      .proposeCanonicalWrite(selectedId, {
        rel_path: proposeDest.trim(),
        evidence_id: proposeFor.id,
        work_item_id: proposeFor.work_item_id,
      })
      .then(() => {
        setProposeFor(null);
        loadDetail();
        setTab("context"); // the pending proposal is now the approval surface
      })
      .catch((err: Error) => setProposeErr(err.message))
      .finally(() => setProposeBusy(false));
  };

  const loadDetail = useCallback(() => {
    if (!selectedId) return;
    api.listWorkItems(selectedId).then(setWorkItems).catch(() => setWorkItems([]));
    api.listContextSources(selectedId).then(setSources).catch(() => setSources([]));
    api.listEvidence(selectedId).then(setEvidence).catch(() => setEvidence([]));
    api.listApprovals({ project_id: selectedId }).then(setApprovals).catch(() => setApprovals([]));
  }, [selectedId]);

  useEffect(() => {
    setWorkItems([]);
    setSources([]);
    setEvidence([]);
    setApprovals([]);
    setError(null);
    setTab("overview");
    loadDetail();
  }, [selectedId, loadDetail]);

  // Re-fetch on tab switch too: proposals/evidence arrive from agents while the
  // panel is open, and the lists are small enough to reload freely.
  useEffect(() => {
    loadDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // ── New project modal ──────────────────────────────────────────────────────
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<ProjectInput>({ name: "", kind: "general", sensitivity: "normal" });
  // S0.4: how the project gets its context root. "managed" (default — server
  // creates one on the data volume) kills the bring-your-own-directory cliff;
  // "path" keeps bind-mounted knowledge repos; "none" defers the choice.
  const [rootMode, setRootMode] = useState<"managed" | "path" | "none">("managed");

  const handleCreateProject = async () => {
    if (!draft.name.trim()) return setError("Project name is required.");
    if (rootMode === "path" && !draft.root_path?.trim()) {
      return setError("Context root path is required (or pick a managed root).");
    }
    setCreating(true);
    setError(null);
    try {
      const p = await api.createProject({
        ...draft,
        name: draft.name.trim(),
        root_path: rootMode === "path" ? draft.root_path?.trim() || null : null,
        create_root: rootMode === "managed",
        description: draft.description?.trim() || null,
      });
      onProjectsChanged();
      setShowCreate(false);
      setDraft({ name: "", kind: "general", sensitivity: "normal" });
      setRootMode("managed");
      setSelectedId(p.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Create failed.");
    } finally {
      setCreating(false);
    }
  };

  // ── Work items ─────────────────────────────────────────────────────────────
  const [showNewWork, setShowNewWork] = useState(false);
  const [workDraft, setWorkDraft] = useState({ title: "", kind: "task", risk: "low", objective: "" });
  const [savingWork, setSavingWork] = useState(false);

  const handleCreateWork = async () => {
    if (!selectedId || !workDraft.title.trim()) return;
    setSavingWork(true);
    setError(null);
    try {
      await api.createWorkItem(selectedId, {
        title: workDraft.title.trim(),
        kind: workDraft.kind.trim() || "task",
        risk: workDraft.risk as "low" | "medium" | "high",
        objective: workDraft.objective,
      });
      setWorkDraft({ title: "", kind: "task", risk: "low", objective: "" });
      setShowNewWork(false);
      loadDetail();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create work item.");
    } finally {
      setSavingWork(false);
    }
  };

  const handleWorkState = async (item: WorkItem, state: WorkItemState) => {
    setError(null);
    try {
      await api.updateWorkItem(item.id, { state });
      loadDetail();
    } catch (e) {
      setError(e instanceof Error ? e.message : "State change refused.");
    }
  };

  // ── Context ────────────────────────────────────────────────────────────────
  const [refreshing, setRefreshing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importWarnings, setImportWarnings] = useState<string[]>([]);

  const handleRefresh = async () => {
    if (!selectedId) return;
    setRefreshing(true);
    setError(null);
    try {
      setSources(await api.refreshContext(selectedId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refresh failed.");
    } finally {
      setRefreshing(false);
    }
  };

  const handleImportManifest = async () => {
    if (!selectedId) return;
    setImporting(true);
    setError(null);
    try {
      const res = await api.declareContextSource(selectedId, null);
      setImportWarnings(res.warnings);
      loadDetail();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Manifest import failed.");
    } finally {
      setImporting(false);
    }
  };

  // ── Approvals (canonical writes) ───────────────────────────────────────────
  const [openDiffId, setOpenDiffId] = useState<number | null>(null);
  const [decidingId, setDecidingId] = useState<number | null>(null);

  const handleDecide = async (a: Approval, approved: boolean) => {
    setDecidingId(a.id);
    setError(null);
    try {
      await api.decideApproval(a.id, approved);
      loadDetail(); // approve applies the write + records decision evidence
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decision failed.");
    } finally {
      setDecidingId(null);
    }
  };

  const pendingWrites = approvals.filter((a) => a.kind === "canonical_write" && a.status === "pending");
  const decidedWrites = approvals.filter((a) => a.kind === "canonical_write" && a.status !== "pending");
  const openWork = workItems.filter((w) => !["done", "dropped"].includes(w.state));

  // ── Render: project list (no selection) ────────────────────────────────────
  if (!selected) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <span className="font-mono text-xs uppercase tracking-widest text-muted">
            Projects · durable work + context, portable across providers
          </span>
          <Button variant="primary" size="sm" icon={<Plus size={13} />} onClick={() => setShowCreate(true)}>
            New project
          </Button>
        </div>

        <div className="stagger grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {projects.map((p) => (
            <Card key={p.id} className="p-4 transition-colors hover:border-brand/40">
              <button className="block w-full text-left" onClick={() => setSelectedId(p.id)}>
                <div className="mb-1.5 flex items-center gap-2">
                  <FolderKanban size={15} className="shrink-0 text-brand" />
                  <span className="min-w-0 flex-1 truncate font-mono text-sm font-semibold text-ink">
                    {p.name}
                  </span>
                  {p.is_default && <Badge tone="brand">default</Badge>}
                </div>
                <div className="mb-2 flex flex-wrap items-center gap-1.5">
                  <Badge>{p.kind}</Badge>
                  <Badge tone={p.status === "active" ? "ok" : "neutral"}>{p.status}</Badge>
                  {p.sensitivity === "confidential" && (
                    <Badge tone="warn"><Lock size={10} /> confidential</Badge>
                  )}
                </div>
                <p className="line-clamp-2 min-h-[2rem] text-xs text-muted">
                  {p.description || "No description."}
                </p>
                <p className="mt-2 font-mono text-[11px] text-muted">updated {fmtTime(p.updated_at)}</p>
              </button>
            </Card>
          ))}
        </div>

        {error && (
          <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</div>
        )}

        {showCreate && (
          <Modal
            open
            onClose={() => setShowCreate(false)}
            title="New project"
            footer={
              <>
                <Button variant="outline" onClick={() => setShowCreate(false)}>Cancel</Button>
                <Button variant="primary" onClick={handleCreateProject} disabled={creating}>
                  {creating ? "Creating…" : "Create project"}
                </Button>
              </>
            }
          >
            <div className="space-y-3">
              <Field label="Name">
                <Input
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="Homelab estate"
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Kind" hint="Free-form label — routing never branches on it.">
                  <Input
                    value={draft.kind ?? ""}
                    onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
                    placeholder="general"
                  />
                </Field>
                <Field label="Sensitivity">
                  <Select
                    value={draft.sensitivity ?? "normal"}
                    onChange={(e) => setDraft({ ...draft, sensitivity: e.target.value })}
                  >
                    <option value="normal">normal</option>
                    <option value="confidential">confidential</option>
                  </Select>
                </Field>
              </div>
              <Field
                label="Context root"
                hint={
                  rootMode === "managed"
                    ? "The server creates the root on the data volume, git-init'd with a starter batonkeep.yaml — backups already cover it."
                    : rootMode === "path"
                      ? "A directory the backend can already reach — e.g. a bind-mounted knowledge repo. Writes to it always go through approval."
                      : "No canonical context for now; you can point the project at a root later."
                }
              >
                <Select
                  value={rootMode}
                  onChange={(e) => setRootMode(e.target.value as typeof rootMode)}
                >
                  <option value="managed">Create a managed root (recommended)</option>
                  <option value="path">Use an existing server path</option>
                  <option value="none">None for now</option>
                </Select>
              </Field>
              {rootMode === "path" && (
                <Field label="Server path">
                  <Input
                    className="font-mono"
                    value={draft.root_path ?? ""}
                    onChange={(e) => setDraft({ ...draft, root_path: e.target.value })}
                    placeholder="/data/projects/homelab"
                  />
                </Field>
              )}
              <Field label="Description">
                <Input
                  value={draft.description ?? ""}
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                />
              </Field>
            </div>
          </Modal>
        )}
      </div>
    );
  }

  // ── Render: project detail ─────────────────────────────────────────────────
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          className="gap-1 px-2"
          icon={<ChevronLeft size={15} />}
          onClick={() => setSelectedId(null)}
        >
          <span className="text-xs">Projects</span>
        </Button>
        <span className="min-w-0 flex-1 truncate font-mono text-sm font-semibold text-ink">
          {selected.name}
        </span>
        {selected.is_default && <Badge tone="brand">default</Badge>}
        {selected.sensitivity === "confidential" && (
          <Badge tone="warn"><Lock size={10} /> confidential</Badge>
        )}
      </div>

      <Tabs
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "work", label: openWork.length > 0 ? `Work · ${openWork.length}` : "Work" },
          { id: "context", label: pendingWrites.length > 0 ? `Context · ${pendingWrites.length}` : "Context" },
          { id: "evidence", label: "Evidence" },
        ] as const}
        active={tab}
        onChange={setTab}
      />

      {error && (
        <div className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</div>
      )}

      {tab === "overview" && (
        <div className="stagger space-y-3">
          <Card className="p-4">
            <p className="mb-3 text-sm text-ink">{selected.description || "No description."}</p>
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs md:grid-cols-3">
              {(
                [
                  ["kind", selected.kind],
                  ["status", selected.status],
                  ["sensitivity", selected.sensitivity],
                  ["context root", selected.root_path ?? "—"],
                  ["manifest", selected.manifest_rel ?? "—"],
                  ["created", fmtTime(selected.created_at)],
                ] as const
              ).map(([k, v]) => (
                <div key={k}>
                  <span className="block font-mono text-[10px] uppercase tracking-wider text-muted">{k}</span>
                  <span className="break-all font-mono text-ink">{v}</span>
                </div>
              ))}
            </div>
          </Card>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {(
              [
                ["open work", openWork.length, "work"],
                ["context sources", sources.length, "context"],
                ["pending approvals", pendingWrites.length, "context"],
                ["evidence", evidence.length, "evidence"],
              ] as const
            ).map(([label, count, target]) => (
              <Card key={label} className="p-3 transition-colors hover:border-brand/40">
                <button className="block w-full text-left" onClick={() => setTab(target)}>
                  <span className="block font-mono text-xl font-semibold text-ink">{count}</span>
                  <span className="font-mono text-[10px] uppercase tracking-wider text-muted">{label}</span>
                </button>
              </Card>
            ))}
          </div>
        </div>
      )}

      {tab === "work" && (
        <div className="stagger space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
              Work items · durable intent, independent of any transcript
            </span>
            <Button
              variant="outline"
              size="sm"
              icon={<Plus size={13} />}
              onClick={() => setShowNewWork((s) => !s)}
            >
              New work item
            </Button>
          </div>

          {showNewWork && (
            <Card className="space-y-3 p-3">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <div className="md:col-span-2">
                  <Field label="Title">
                    <Input
                      value={workDraft.title}
                      onChange={(e) => setWorkDraft({ ...workDraft, title: e.target.value })}
                      placeholder="Upgrade reverse proxy"
                    />
                  </Field>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Kind">
                    <Input
                      value={workDraft.kind}
                      onChange={(e) => setWorkDraft({ ...workDraft, kind: e.target.value })}
                      placeholder="task"
                    />
                  </Field>
                  <Field label="Risk">
                    <Select
                      value={workDraft.risk}
                      onChange={(e) => setWorkDraft({ ...workDraft, risk: e.target.value })}
                    >
                      <option value="low">low</option>
                      <option value="medium">medium</option>
                      <option value="high">high</option>
                    </Select>
                  </Field>
                </div>
              </div>
              <Field label="Objective — what done means">
                <Input
                  value={workDraft.objective}
                  onChange={(e) => setWorkDraft({ ...workDraft, objective: e.target.value })}
                  placeholder="Proxy on the new version, all services reachable, rollback path noted"
                />
              </Field>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" size="sm" onClick={() => setShowNewWork(false)}>Cancel</Button>
                <Button variant="primary" size="sm" onClick={handleCreateWork} disabled={savingWork || !workDraft.title.trim()}>
                  {savingWork ? "Creating…" : "Create"}
                </Button>
              </div>
            </Card>
          )}

          {workItems.length === 0 && !showNewWork && (
            <div className="rounded-xl border border-dashed border-edge p-8 text-center">
              <p className="mb-1 font-mono text-sm font-semibold text-ink">No work items yet</p>
              <p className="text-xs text-muted">
                A work item carries objective + next action durably, so any provider can pick the work up cold.
              </p>
            </div>
          )}

          {workItems.map((w) => (
            <Card key={w.id} className="p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[11px] text-muted">#{w.id}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">{w.title}</span>
                <Badge>{w.kind}</Badge>
                <Badge tone={RISK_TONE[w.risk] ?? "neutral"}>{w.risk}</Badge>
                <Badge tone={WORK_STATE_TONE[w.state]}>{w.state.replace(/_/g, " ")}</Badge>
                <Select
                  className="!h-8 w-40 text-xs"
                  value=""
                  onChange={(e) => {
                    if (e.target.value) handleWorkState(w, e.target.value as WorkItemState);
                  }}
                  aria-label={`Change state of work item ${w.id}`}
                >
                  <option value="">move to…</option>
                  {(WORK_ITEM_TRANSITIONS[w.state] ?? []).map((s) => (
                    <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
                  ))}
                </Select>
              </div>
              {(w.objective || w.next_action) && (
                <div className="mt-2 space-y-1 text-xs">
                  {w.objective && (
                    <p className="text-muted">
                      <span className="font-mono text-[10px] uppercase tracking-wider">objective · </span>
                      <span className="text-ink">{w.objective}</span>
                    </p>
                  )}
                  {w.next_action && (
                    <p className="text-muted">
                      <span className="font-mono text-[10px] uppercase tracking-wider">next · </span>
                      <span className="text-ink">{w.next_action}</span>
                    </p>
                  )}
                </div>
              )}
              <SubtaskChecklist item={w} onChanged={loadDetail} />
            </Card>
          ))}
        </div>
      )}

      {tab === "context" && (
        <div className="stagger space-y-4">
          {/* Pending canonical-write proposals — the human approval surface. */}
          <div className="space-y-2">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
              Pending canonical writes · approving applies the diff to the context root
            </span>
            {pendingWrites.length === 0 && (
              <div className="rounded-lg border border-dashed border-edge p-4 text-center text-xs text-muted">
                No pending proposals. Agents (and the API) propose edits to the context root; nothing is
                written until you approve it here.
              </div>
            )}
            {pendingWrites.map((a) => {
              const rel = String(a.payload?.rel_path ?? "");
              const diff = String(a.payload?.diff ?? "");
              const open = openDiffId === a.id;
              return (
                <Card key={a.id} active className="p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <FileText size={14} className="shrink-0 text-brand" />
                    <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">{rel}</span>
                    <Badge tone="warn">pending</Badge>
                    <span className="font-mono text-[11px] text-muted">
                      by {a.producer} · {fmtTime(a.created_at)}
                    </span>
                  </div>
                  <button
                    className="mt-2 flex items-center gap-1 font-mono text-[11px] text-brand"
                    onClick={() => setOpenDiffId(open ? null : a.id)}
                  >
                    {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    {open ? "hide diff" : "show diff"}
                  </button>
                  {open && <div className="mt-2"><DiffView diff={diff} /></div>}
                  <div className="mt-3 flex justify-end gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      className="hover:border-bad/50 hover:text-bad"
                      onClick={() => handleDecide(a, false)}
                      disabled={decidingId === a.id}
                    >
                      Deny
                    </Button>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => handleDecide(a, true)}
                      disabled={decidingId === a.id}
                    >
                      {decidingId === a.id ? "Applying…" : "Approve + apply"}
                    </Button>
                  </div>
                </Card>
              );
            })}
            {decidedWrites.length > 0 && (
              <div className="space-y-1">
                {decidedWrites.slice(0, 10).map((a) => (
                  <div
                    key={a.id}
                    className="flex items-center gap-2 rounded-lg border border-edge bg-panel/60 px-3 py-1.5 text-xs"
                  >
                    <span className="min-w-0 flex-1 truncate font-mono text-ink">
                      {String(a.payload?.rel_path ?? a.request_id)}
                    </span>
                    <Badge tone={a.status === "approved" ? "ok" : a.status === "denied" ? "bad" : "neutral"}>
                      {a.status}
                    </Badge>
                    <span className="font-mono text-[11px] text-muted">
                      {a.decided_by ?? "—"} · {fmtTime(a.decided_at ?? a.created_at)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Declared sources + freshness. */}
          <div className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
                Context sources · staleness is surfaced, never silently rewritten
              </span>
              <div className="flex gap-2">
                {selected.root_path && (
                  <Button variant="outline" size="sm" onClick={handleImportManifest} disabled={importing}>
                    {importing ? "Importing…" : "Import from manifest"}
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  icon={<RefreshCw size={13} className={refreshing ? "animate-spin" : ""} />}
                  onClick={handleRefresh}
                  disabled={refreshing || sources.length === 0}
                >
                  Refresh freshness
                </Button>
              </div>
            </div>
            {importWarnings.length > 0 && (
              <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
                {importWarnings.map((w, i) => <p key={i}>{w}</p>)}
              </div>
            )}
            {!selected.root_path && (
              <div className="rounded-lg border border-dashed border-edge p-4 text-center text-xs text-muted">
                This project has no context root, so there are no sources to declare.
              </div>
            )}
            {sources.map((s) => {
              // File sources with a text-ish extension open in the same viewer
              // modal as evidence (S0.5); dir/git sources have no single file
              // to show, so they keep the plain row.
              const viewable = s.kind === "file" && evidenceViewKind(s.rel_path) != null;
              return (
                <div
                  key={s.id}
                  onClick={viewable ? () => openSource(s) : undefined}
                  title={viewable ? "View source" : undefined}
                  className={
                    "flex flex-wrap items-center gap-2 rounded-lg border border-edge bg-panel/60 px-3 py-2" +
                    (viewable ? " cursor-pointer transition-colors hover:border-brand/40" : "")
                  }
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">{s.rel_path}</span>
                  <Badge>{s.kind}</Badge>
                  {s.domain && <Badge tone="defer">{s.domain}</Badge>}
                  {s.sensitivity !== "inherit" && <Badge tone="warn">{s.sensitivity}</Badge>}
                  <span className="font-mono text-[11px] text-muted">
                    {s.last_revision ? s.last_revision.slice(0, 12) : "unhashed"} ·{" "}
                    {s.last_checked_at ? `checked ${fmtTime(s.last_checked_at)}` : "never checked"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {tab === "evidence" && (
        <div className="stagger space-y-2">
          <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
            Evidence · append-only outcome record (digest pinned at capture)
          </span>
          {evidence.length === 0 && (
            <div className="rounded-xl border border-dashed border-edge p-8 text-center">
              <p className="mb-1 font-mono text-sm font-semibold text-ink">No evidence yet</p>
              <p className="text-xs text-muted">
                Task-run reports, session diffs, and approval decisions land here automatically as runs
                complete in this project.
              </p>
            </div>
          )}
          {evidence.map((e) => {
            const viewable = evidenceViewKind(e.rel_path) != null;
            return (
              <div
                key={e.id}
                onClick={viewable ? () => openEvidence(e) : undefined}
                title={viewable ? "View evidence" : undefined}
                className={
                  "flex flex-wrap items-center gap-2 rounded-lg border border-edge bg-panel/60 px-3 py-2" +
                  (viewable ? " cursor-pointer transition-colors hover:border-brand/40" : "")
                }
              >
                <Badge tone={EVIDENCE_TONE[e.kind] ?? "neutral"}>{e.kind}</Badge>
                <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">
                  {e.rel_path.split("/").pop()}
                </span>
                {e.work_item_id != null && (
                  <span className="font-mono text-[11px] text-muted">work #{e.work_item_id}</span>
                )}
                <span className="font-mono text-[11px] text-muted">
                  {e.producer} · {fmtBytes(e.bytes)} · {fmtTime(e.created_at)}
                </span>
                <a
                  href={api.evidenceRawUrl(e.id)}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(ev) => ev.stopPropagation()}
                  className="flex items-center gap-1 font-mono text-[11px] text-brand hover:opacity-80"
                  title="Open the raw evidence file"
                >
                  <ExternalLink size={12} /> raw
                </a>
                {selected.root_path && (
                  <button
                    onClick={(ev) => {
                      ev.stopPropagation();
                      openPropose(e);
                    }}
                    className="flex items-center gap-1 rounded-md border border-edge px-1.5 py-0.5 font-mono text-[11px] text-muted transition hover:border-brand/40 hover:text-ink"
                    title="Propose this evidence into the canonical context root (needs your approval to apply)"
                  >
                    <FileText size={11} /> propose
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Document viewer (evidence + context sources): markdown rendered,
          diffs colorized, other text as-is. */}
      <Modal
        open={viewing != null}
        onClose={() => {
          setViewing(null);
          setViewText(null);
        }}
        title={viewing?.name}
        size="max-w-2xl"
      >
        {viewing && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {viewing.badge && (
                <Badge tone={viewing.badgeTone ?? "neutral"}>{viewing.badge}</Badge>
              )}
              <span className="min-w-0 flex-1 font-mono text-[11px] text-muted">
                {viewing.meta}
              </span>
              <a
                href={viewing.url}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 font-mono text-[11px] text-brand hover:opacity-80"
                title="Open the raw file"
              >
                <ExternalLink size={12} /> raw
              </a>
            </div>
            {viewText == null ? (
              <p className="font-mono text-xs text-muted">Loading…</p>
            ) : evidenceViewKind(viewing.relPath) === "markdown" ? (
              <div className="markdown" dangerouslySetInnerHTML={{ __html: viewHtml }} />
            ) : evidenceViewKind(viewing.relPath) === "diff" ? (
              <DiffView diff={viewText} />
            ) : (
              <pre className="overflow-auto whitespace-pre-wrap rounded-lg border border-edge bg-base/60 p-3 font-mono text-[11px] leading-relaxed">
                {viewText}
              </pre>
            )}
          </div>
        )}
      </Modal>

      {/* Propose evidence → canonical context (S0.5): by-reference — the bytes
          stay in the evidence store, digest-pinned; approval applies it. */}
      <Modal
        open={proposeFor != null}
        onClose={() => setProposeFor(null)}
        title="Propose to canonical context"
        size="max-w-md"
      >
        {proposeFor && (
          <div className="space-y-3">
            <p className="text-xs text-muted">
              Creates a pending canonical-write proposal from{" "}
              <span className="font-mono text-ink">
                {proposeFor.rel_path.split("/").pop()}
              </span>{" "}
              (digest-pinned — the content is re-verified when you approve).
              Nothing is written until it is approved on the Context tab.
            </p>
            <Field label="Destination path in the context root">
              <Input
                value={proposeDest}
                onChange={(e) => setProposeDest(e.target.value)}
                placeholder="docs/methodology.md"
              />
            </Field>
            {proposeErr && (
              <p className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">
                {proposeErr}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setProposeFor(null)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={submitPropose}
                disabled={proposeBusy || !proposeDest.trim()}
              >
                {proposeBusy ? "Proposing…" : "Create proposal"}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
