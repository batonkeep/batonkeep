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
  Sparkles,
} from "lucide-react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api } from "../api";
import type {
  Approval,
  ApprovalBatchResult,
  ContextCoverage,
  ContextSource,
  Evidence,
  PlannerRun,
  PlannerSettings,
  Project,
  ProjectInput,
  ProviderHealth,
  WorkItem,
  WorkItemState,
} from "../types";
import { WORK_ITEM_TRANSITIONS } from "../types";
import { Badge, Button, Card, Field, Input, Modal, Select, Tabs, type Tone } from "../ui";
import { fmtCost, fmtTime } from "../format";
import SubtaskChecklist from "./SubtaskChecklist";

interface Props {
  projects: Project[];
  onProjectsChanged: () => void;
}

type Tab = "overview" | "work" | "context" | "evidence";

const WORK_STATE_TONE: Record<WorkItemState, Tone> = {
  proposed: "brand",
  open: "neutral",
  in_progress: "live",
  awaiting_approval: "warn",
  blocked: "bad",
  done: "ok",
  dropped: "neutral",
  reopened: "defer",
};

const RISK_TONE: Record<string, Tone> = { low: "neutral", medium: "warn", high: "bad" };

const PLANNER_RUN_TONE: Record<string, Tone> = {
  running: "live",
  succeeded: "ok",
  failed: "bad",
};

// One line for what a planning turn produced. Project turns carry a digest headline;
// item turns are summarized from their counts. A succeeded turn that proposed nothing
// says so — silence would read as "not run yet".
function plannerRunOutcome(r: PlannerRun): string {
  const subtasks = Number(r.proposals?.subtasks_proposed ?? 0);
  const items = r.proposals?.work_items_proposed?.length ?? 0;
  const parts: string[] = [];
  if (subtasks) parts.push(`${subtasks} sub-task(s)`);
  if (items) parts.push(`${items} work item(s)`);
  return parts.length ? `proposed ${parts.join(" · ")}` : "proposed nothing";
}

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
  const [coverage, setCoverage] = useState<ContextCoverage | null>(null);
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
    api.getContextCoverage(selectedId).then(setCoverage).catch(() => setCoverage(null));
    api.listProjectPlannerRuns(selectedId).then(setPlannerHistory).catch(() => setPlannerHistory([]));
  }, [selectedId]);

  useEffect(() => {
    setWorkItems([]);
    setSources([]);
    setEvidence([]);
    setApprovals([]);
    setCoverage(null);
    setDeclaredNote(null);
    // Clear the planner surfaces too, so switching projects never shows the previous
    // project's selection or spend while the new one loads.
    setPlannerSettings(null);
    setPlannerHistory([]);
    setProjectRun(null);
    setPlannerRuns({});
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

  // ── Planner lane (P-0078) ──────────────────────────────────────────────────
  // Proposer-only, in two scopes. An item turn lands `proposed` sub-tasks + a
  // suggested next action on the item (confirmed in the checklist below it) and may
  // propose child work items; a project turn reads the ledger, records a digest, and
  // triages missing work into `proposed` items. Nothing it produces is live until
  // the operator accepts it.
  const [planningId, setPlanningId] = useState<number | null>(null);
  const [planningProject, setPlanningProject] = useState(false);
  const [plannerRuns, setPlannerRuns] = useState<Record<number, PlannerRun>>({});
  const [projectRun, setProjectRun] = useState<PlannerRun | null>(null);
  const [plannerHistory, setPlannerHistory] = useState<PlannerRun[]>([]);
  const [openRunId, setOpenRunId] = useState<number | null>(null);
  const planning = planningId != null || planningProject;

  // Selection settings (slice 3). `settings` is the server's resolution — stored
  // default *and* what would actually run once fallback and the sovereignty fence
  // are applied — so this panel can never disagree with the lane about what runs.
  const [plannerSettings, setPlannerSettings] = useState<PlannerSettings | null>(null);
  const [plannerDraft, setPlannerDraft] = useState({ provider: "", model: "" });
  const [savingPlanner, setSavingPlanner] = useState(false);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);

  useEffect(() => {
    if (!selectedId) return;
    api.getPlannerSettings(selectedId)
      .then((s) => {
        setPlannerSettings(s);
        setPlannerDraft({ provider: s.provider ?? "", model: s.model ?? "" });
      })
      .catch(() => setPlannerSettings(null));
  }, [selectedId]);

  useEffect(() => {
    api.listProviders().then(setProviders).catch(() => setProviders([]));
  }, []);

  const handleSavePlanner = async () => {
    if (!selectedId) return;
    setSavingPlanner(true);
    setError(null);
    try {
      const next = await api.setPlannerSettings(selectedId, {
        provider: plannerDraft.provider || null,
        // A model without a provider is refused server-side; drop it so clearing
        // the provider reads as "clear the whole default", which is what it means.
        model: plannerDraft.provider ? plannerDraft.model || null : null,
      });
      setPlannerSettings(next);
      setPlannerDraft({ provider: next.provider ?? "", model: next.model ?? "" });
      onProjectsChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the planner default.");
    } finally {
      setSavingPlanner(false);
    }
  };

  const plannerDirty =
    plannerDraft.provider !== (plannerSettings?.provider ?? "") ||
    plannerDraft.model !== (plannerSettings?.model ?? "");

  // The lane drives in the background; poll to completion. The budget outlasts the
  // server's own planner timeout, so a turn that is going to fail gets to *say* so
  // rather than having us give up first and leave a spinner on screen — the exact
  // way this surface lied to the operator before. Tight ticks at the start (a local
  // model answers fast), slower after.
  const pollRun = async (run: PlannerRun, onTick: (r: PlannerRun) => void) => {
    let cur = run;
    onTick(cur);
    for (let i = 0; i < 100 && cur.status === "running"; i++) {
      await new Promise((r) => setTimeout(r, i < 10 ? 1000 : 3000));
      try {
        cur = await api.getPlannerRun(cur.id);
      } catch {
        break; // keep the last known state; the history list stays authoritative
      }
    }
    onTick(cur);
    loadDetail(); // pick up whatever the planner proposed, and refresh the history
  };

  const handlePlan = async (item: WorkItem) => {
    setPlanningId(item.id);
    setError(null);
    try {
      const run = await api.planWorkItem(item.id);
      await pollRun(run, (r) => setPlannerRuns((m) => ({ ...m, [item.id]: r })));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Planning turn failed to start.");
    } finally {
      setPlanningId(null);
    }
  };

  const handlePlanProject = async () => {
    if (!selectedId) return;
    setPlanningProject(true);
    setError(null);
    try {
      const run = await api.planProject(selectedId);
      await pollRun(run, setProjectRun);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Planning turn failed to start.");
    } finally {
      setPlanningProject(false);
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
  // P-0073: approving also declares the path as a context source. Opt-out is
  // per-proposal and default-off (i.e. declaring is the default) — the failure
  // it prevents is silent, the cost of an unwanted row is visible.
  const [skipDeclare, setSkipDeclare] = useState<Record<number, boolean>>({});
  const [declaredNote, setDeclaredNote] = useState<string | null>(null);

  const handleDecide = async (a: Approval, approved: boolean) => {
    setDecidingId(a.id);
    setError(null);
    try {
      const res = await api.decideApproval(a.id, approved, !skipDeclare[a.id]);
      const declared = res.applied?.declared_source as { rel_path?: string } | null;
      setDeclaredNote(
        declared?.rel_path
          ? `Declared ${declared.rel_path} as a context source — sessions will now receive it.`
          : null,
      );
      loadDetail(); // approve applies the write + records decision evidence
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decision failed.");
    } finally {
      setDecidingId(null);
    }
  };

  // ── Batch decisions (P-0077) ───────────────────────────────────────────────
  // Selection is the gesture that collapses; the review is not skipped. The
  // set is named explicitly — there is no "approve everything" affordance.
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [batching, setBatching] = useState(false);
  const [batchResult, setBatchResult] = useState<ApprovalBatchResult | null>(null);
  // The batch carries its own declare choice rather than deriving one from the
  // per-row boxes — a mixed selection has no honest single answer, and the
  // backend takes one flag for the set.
  const [batchDeclare, setBatchDeclare] = useState(true);

  const toggleSelected = (id: number) =>
    setSelectedIds((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const handleBatchDecide = async (approved: boolean) => {
    if (selectedIds.length === 0) return;
    setBatching(true);
    setError(null);
    setBatchResult(null);
    try {
      const res = await api.batchDecideApprovals(selectedIds, approved, batchDeclare);
      setBatchResult(res);
      setSelectedIds(res.results.filter((r) => r.outcome === "failed").map((r) => r.approval_id));
      loadDetail();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Batch decision failed.");
    } finally {
      setBatching(false);
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

          {/* Planner default (P-0078). Optional by design — a planner is never a
              mandatory per-project decision, so "no default" is a valid answer and
              the fallback is shown rather than hidden. */}
          <Card className="space-y-3 p-4">
            <div className="flex items-center gap-2">
              <Sparkles size={14} className="shrink-0 text-brand" />
              <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
                Planner · the model that proposes plans for this project
              </span>
            </div>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Field label="Provider — leave empty to use the first available">
                <Select
                  value={plannerDraft.provider}
                  onChange={(e) =>
                    setPlannerDraft({ ...plannerDraft, provider: e.target.value })
                  }
                >
                  <option value="">no default (fall back)</option>
                  {providers.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.label} · {p.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Model — optional, else the provider's own default">
                <Input
                  value={plannerDraft.model}
                  onChange={(e) => setPlannerDraft({ ...plannerDraft, model: e.target.value })}
                  disabled={!plannerDraft.provider}
                  placeholder={plannerDraft.provider ? "provider default" : "pick a provider first"}
                />
              </Field>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {plannerSettings && (
                <span className="text-xs text-muted">
                  <span className="font-mono text-[10px] uppercase tracking-wider">runs as · </span>
                  <span className="font-mono text-ink">
                    {plannerSettings.effective_provider ?? "nothing available"}
                    {plannerSettings.effective_model && ` / ${plannerSettings.effective_model}`}
                  </span>
                  {plannerSettings.local_pinned && (
                    <Badge tone="ok" className="ml-2">
                      <Lock size={10} /> local-pinned
                    </Badge>
                  )}
                </span>
              )}
              <Button
                className="ml-auto"
                variant="primary"
                size="sm"
                onClick={handleSavePlanner}
                disabled={savingPlanner || !plannerDirty}
              >
                {savingPlanner ? "Saving…" : "Save"}
              </Button>
            </div>
            {plannerSettings?.note && (
              <p className="text-xs text-muted">{plannerSettings.note}</p>
            )}
          </Card>

          {/* Planning-turn history: the audit + spend trail. Planning is meant to be
              cheap, frequent meta-work — showing what it costs is how that stays true. */}
          {plannerHistory.length > 0 && (
            <Card className="p-4">
              <div className="mb-2 flex items-center gap-2">
                <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
                  Planning turns · audit + spend
                </span>
                <span className="ml-auto font-mono text-[11px] text-muted">
                  {plannerHistory.length} shown ·{" "}
                  {fmtCost(plannerHistory.reduce((t, r) => t + r.cost_usd, 0))}
                </span>
              </div>
              <div className="space-y-1">
                {plannerHistory.map((r) => (
                  <div key={r.id}>
                    <button
                      className="flex w-full flex-wrap items-center gap-2 text-left text-xs"
                      onClick={() => setOpenRunId(openRunId === r.id ? null : r.id)}
                      title="Show what this turn was given and what it answered"
                    >
                      <Badge tone={PLANNER_RUN_TONE[r.status] ?? "neutral"}>{r.status}</Badge>
                      <span className="font-mono text-[11px] text-muted">
                        {r.work_item_id != null ? `#${r.work_item_id}` : "project"}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-muted">
                        {r.status === "failed"
                          ? r.error ?? "failed"
                          : r.status === "running"
                            ? "in flight…"
                            : r.proposals?.summary?.headline ?? plannerRunOutcome(r)}
                      </span>
                      <span className="font-mono text-[10px] text-muted">
                        {r.model ?? r.provider}
                        {r.local_pinned && " · local"}
                      </span>
                      <span className="font-mono text-[10px] text-muted">{fmtCost(r.cost_usd)}</span>
                      <span className="font-mono text-[10px] text-muted">{fmtTime(r.created_at)}</span>
                      <ChevronDown
                        size={12}
                        className={`shrink-0 text-muted transition-transform ${
                          openRunId === r.id ? "rotate-180" : ""
                        }`}
                      />
                    </button>
                    {/* A turn that proposed nothing is almost always a turn that was
                        told nothing. Showing the exact prompt makes that visible
                        instead of leaving the planner looking broken. */}
                    {openRunId === r.id && (
                      <div className="mt-1 space-y-2 rounded-lg border border-edge bg-base/60 p-2">
                        <div>
                          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
                            what it was given
                          </span>
                          <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-ink">
                            {r.request ?? "—"}
                          </pre>
                        </div>
                        {r.response && (
                          <div>
                            <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
                              what it answered
                            </span>
                            <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-ink">
                              {r.response}
                            </pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      )}

      {tab === "work" && (
        <div className="stagger space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
              Work items · durable intent, independent of any transcript
            </span>

            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                icon={<Sparkles size={13} />}
                onClick={handlePlanProject}
                disabled={planning}
                title="Read this project's open ledger — the planner records a status digest and proposes work items for anything missing"
              >
                {planningProject ? "Planning…" : "Plan project"}
              </Button>
              <Button
                variant="outline"
                size="sm"
                icon={<Plus size={13} />}
                onClick={() => setShowNewWork((s) => !s)}
              >
                New work item
              </Button>
            </div>
          </div>

          {/* The two planner scopes are not discoverable from the button labels
              alone — say which one runs what, so "where do I trigger a summary?"
              has an answer on the screen where the answer lives. */}
          <p className="text-xs text-muted">
            <span className="font-mono text-[10px] uppercase tracking-wider">planner · </span>
            <span className="text-ink">Plan project</span> summarizes this project's ledger and
            triages missing work into proposals · <span className="text-ink">Plan with agent</span>{" "}
            on a work item proposes its sub-tasks, its next action, and any child work items.
            Everything it produces waits for you to accept it.
          </p>

          {projectRun && (
            <Card className="p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Sparkles size={13} className="shrink-0 text-brand" />
                <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
                  Planner digest
                </span>
                <span className="ml-auto font-mono text-[10px] text-muted">
                  {projectRun.model ?? projectRun.provider}
                  {projectRun.local_pinned && " · local-pinned"}
                </span>
              </div>
              {/* A `running` row we are no longer polling is not "in progress" from
                  the operator's side — say so, and point at where the answer lands,
                  instead of showing a spinner that never resolves. */}
              {projectRun.status === "running" && (
                <p className="mt-2 text-xs text-muted">
                  {planningProject
                    ? "Reading the ledger…"
                    : "Still running on the server. It finishes in the background — " +
                      "reopen this project to see the result in the planning-turn history."}
                </p>
              )}
              {projectRun.status === "failed" && (
                <p className="mt-2 text-xs text-bad">{projectRun.error ?? "Planning turn failed."}</p>
              )}
              {projectRun.status === "succeeded" && (
                <div className="mt-2 space-y-1 text-xs">
                  <p className="text-ink">
                    {projectRun.proposals?.summary?.headline ?? "No digest recorded."}
                  </p>
                  {projectRun.proposals?.summary?.notes && (
                    <p className="text-muted">{projectRun.proposals.summary.notes}</p>
                  )}
                  {!!projectRun.proposals?.summary?.focus?.length && (
                    <p className="text-muted">
                      <span className="font-mono text-[10px] uppercase tracking-wider">focus · </span>
                      {projectRun.proposals.summary.focus.map((n) => `#${n}`).join(" ")}
                    </p>
                  )}
                  {!!projectRun.proposals?.summary?.stalled?.length && (
                    <p className="text-muted">
                      <span className="font-mono text-[10px] uppercase tracking-wider">stalled · </span>
                      {projectRun.proposals.summary.stalled.map((n) => `#${n}`).join(" ")}
                    </p>
                  )}
                  {!!projectRun.proposals?.work_items_proposed?.length && (
                    <p className="text-muted">
                      Proposed {projectRun.proposals.work_items_proposed.length} work item(s) below —
                      accept or reject each one.
                    </p>
                  )}
                </div>
              )}
            </Card>
          )}

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
                {/* A proposal is a decision, not a state change: give the operator the
                    two edges out of `proposed` directly rather than burying accept in
                    a "move to…" list. Planning a proposal is premature — it may not
                    become work at all — so the planner button waits for acceptance. */}
                {w.state === "proposed" ? (
                  <>
                    <Button variant="primary" size="sm"
                            onClick={() => handleWorkState(w, "open")}>
                      Accept
                    </Button>
                    <Button variant="ghost" size="sm"
                            onClick={() => handleWorkState(w, "dropped")}>
                      Reject
                    </Button>
                  </>
                ) : (
                  <>
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
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handlePlan(w)}
                      disabled={planning}
                      title="Run a planning turn — the planner proposes sub-tasks and a next action; you confirm them"
                    >
                      <Sparkles size={13} />
                      {planningId === w.id ? "Planning…" : "Plan with agent"}
                    </Button>
                  </>
                )}
              </div>
              {w.state === "proposed" && (
                <p className="mt-2 text-xs text-muted">
                  Proposed by the planner{w.parent_id != null && <> · child of #{w.parent_id}</>}
                  {" "}— nothing runs against it until you accept it.
                </p>
              )}
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
              {plannerRuns[w.id] && (
                <p className="mt-2 text-xs text-muted">
                  <span className="font-mono text-[10px] uppercase tracking-wider">planner · </span>
                  {plannerRuns[w.id].status === "failed" ? (
                    <span className="text-bad">{plannerRuns[w.id].error ?? "failed"}</span>
                  ) : (
                    <span className="text-ink">
                      {plannerRuns[w.id].status === "running"
                        ? planningId === w.id
                          ? "thinking…"
                          : "still running on the server — check the planning-turn history"
                        : [
                            `proposed ${Number(plannerRuns[w.id].proposals?.subtasks_proposed ?? 0)} sub-task(s)`,
                            ...(plannerRuns[w.id].proposals?.work_items_proposed?.length
                              ? [`${plannerRuns[w.id].proposals!.work_items_proposed!.length} child work item(s)`]
                              : []),
                          ].join(" · ")}
                    </span>
                  )}
                  <span className="ml-1 font-mono text-[10px]">
                    {plannerRuns[w.id].model ?? plannerRuns[w.id].provider}
                    {plannerRuns[w.id].local_pinned && " · local-pinned"}
                  </span>
                </p>
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
            {/* Batch toolbar (P-0077): promoting a coherent set costs one
                decision instead of N. Only shown when there is a set. */}
            {pendingWrites.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 rounded-lg border border-edge bg-panel/60 px-3 py-2">
                <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
                  <input
                    type="checkbox"
                    className="accent-brand"
                    checked={selectedIds.length === pendingWrites.length}
                    ref={(el) => {
                      if (el)
                        el.indeterminate =
                          selectedIds.length > 0 && selectedIds.length < pendingWrites.length;
                    }}
                    onChange={(e) =>
                      setSelectedIds(e.target.checked ? pendingWrites.map((a) => a.id) : [])
                    }
                  />
                  Select all {pendingWrites.length}
                </label>
                <span className="flex-1 font-mono text-[11px] text-muted">
                  {selectedIds.length > 0
                    ? `${selectedIds.length} selected · decided as one audit event`
                    : "select proposals to decide them together"}
                </span>
                <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted">
                  <input
                    type="checkbox"
                    className="accent-brand"
                    checked={batchDeclare}
                    onChange={(e) => setBatchDeclare(e.target.checked)}
                  />
                  declare sources
                </label>
                <Button
                  variant="outline"
                  size="sm"
                  className="hover:border-bad/50 hover:text-bad"
                  onClick={() => handleBatchDecide(false)}
                  disabled={batching || selectedIds.length === 0}
                >
                  Deny {selectedIds.length || ""}
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => handleBatchDecide(true)}
                  disabled={batching || selectedIds.length === 0}
                >
                  {batching ? "Applying…" : `Approve ${selectedIds.length || ""}`}
                </Button>
              </div>
            )}
            {batchResult && (
              <div
                className={`rounded-lg border px-3 py-2 text-xs ${
                  batchResult.failed > 0
                    ? "border-warn/40 bg-warn/10 text-warn"
                    : "border-ok/40 bg-ok/10 text-ok"
                }`}
              >
                <p className="font-semibold">
                  {batchResult.decided} {batchResult.approved ? "approved" : "denied"}
                  {batchResult.failed > 0 && `, ${batchResult.failed} failed`} · batch{" "}
                  {batchResult.batch_id.slice(0, 12)}
                </p>
                {/* Failed rows stay pending and are still selected, so the
                    operator can retry them without hunting for them. */}
                {batchResult.results
                  .filter((r) => r.outcome === "failed")
                  .map((r) => (
                    <p key={r.approval_id} className="mt-1 font-mono opacity-80">
                      {r.rel_path ?? `#${r.approval_id}`}: {r.error}
                    </p>
                  ))}
              </div>
            )}
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
                    {pendingWrites.length > 0 && (
                      <input
                        type="checkbox"
                        className="shrink-0 cursor-pointer accent-brand"
                        checked={selectedIds.includes(a.id)}
                        onChange={() => toggleSelected(a.id)}
                        aria-label={`Select ${rel}`}
                      />
                    )}
                    <FileText size={14} className="shrink-0 text-brand" />
                    <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">{rel}</span>
                    {/* Whole file bodies, not patches: approving a proposal
                        written against an older version of the file discards
                        whatever landed underneath it. */}
                    {a.stale && (
                      <span title="The file changed after this was proposed — approving it will discard those changes">
                        <Badge tone="warn">stale</Badge>
                      </span>
                    )}
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
                  {/* Approving canon and making it reach sessions used to be two
                      separate acts, and the gap was silent — one decision now
                      does both unless the approver says otherwise. */}
                  <label className="mt-3 flex cursor-pointer items-start gap-2 text-[11px] text-muted">
                    <input
                      type="checkbox"
                      className="mt-0.5 accent-brand"
                      checked={!skipDeclare[a.id]}
                      onChange={(e) =>
                        setSkipDeclare((s) => ({ ...s, [a.id]: !e.target.checked }))
                      }
                    />
                    <span>
                      Declare as a context source so later sessions receive it.
                      Skipped automatically if an existing source already covers this path.
                    </span>
                  </label>
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
            {declaredNote && (
              <div className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-xs text-ok">
                {declaredNote}
              </div>
            )}
            {/* P-0073: a projection that is a strict subset of the root is
                otherwise silent until an agent goes looking for what it was
                briefed to read. Shown before a session runs, not after. */}
            {coverage && coverage.undeclared_count > 0 && (
              <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
                <p className="font-semibold">
                  {coverage.undeclared_count}
                  {coverage.truncated ? "+" : ""} file
                  {coverage.undeclared_count === 1 ? "" : "s"} in the context root
                  {coverage.declared_count === 0
                    ? " are not declared as sources"
                    : " are not covered by any declared source"}
                  .
                </p>
                <p className="mt-1 opacity-80">
                  Sessions receive only declared sources, so this material is not in
                  their projection — declare it below, or from the manifest.
                </p>
                <p className="mt-1 font-mono opacity-70">
                  {coverage.sample.join(" · ")}
                  {coverage.undeclared_count > coverage.sample.length && " · …"}
                </p>
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
