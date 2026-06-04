// App.tsx — top-level shell: data orchestration, view routing, modals.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Moon, Plus, Settings2, Sun } from "lucide-react";
import { api } from "./api";
import { useLiveFeed } from "./useLiveFeed";
import type { Credential, Mode, ProviderHealth, Run, Session, Stats, Task, TaskInput } from "./types";
import Sidebar, { View } from "./components/Sidebar";
import StatsBar from "./components/StatsBar";
import TaskList from "./components/TaskList";
import TaskForm from "./components/TaskForm";
import RunViewer from "./components/RunViewer";
import SessionView from "./components/SessionView";
import ProvidersPanel from "./components/ProvidersPanel";
import Onboarding from "./components/Onboarding";
import Styleguide from "./components/Styleguide";
import { Button, Logo, Tabs } from "./ui";
import { STATUS_META, fmtTime } from "./format";

function runsPerDay(runs: Run[], days = 14): number[] {
  const buckets = new Array(days).fill(0);
  const dayMs = 86_400_000;
  const start = new Date().setHours(0, 0, 0, 0) - (days - 1) * dayMs;
  for (const r of runs) {
    const idx = Math.floor((new Date(r.created_at).getTime() - start) / dayMs);
    if (idx >= 0 && idx < days) buckets[idx]++;
  }
  return buckets;
}

export default function App() {
  // Design-system reference (D-track). Reachable at #styleguide; re-renders on
  // hash change so it works without a router.
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  if (hash === "#styleguide") return <Styleguide />;

  return <AppShell />;
}

function AppShell() {
  const { status: wsStatus, liveRuns } = useLiveFeed();
  const [view, setView] = useState<View>("tasks");
  const [tasksTab, setTasksTab] = useState<"tasks" | "live">("tasks");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  // Scoped console: available only when the backend enables it; token is entered
  // by the operator and kept in React state (never persisted).
  const [consoleAvailable, setConsoleAvailable] = useState(false);
  const [consoleToken, setConsoleToken] = useState("");

  const [editingTask, setEditingTask] = useState<Task | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null);

  // Theme — light is the default; persisted best-effort so it survives reloads.
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      return localStorage.getItem("theme") === "dark" ? "dark" : "light";
    } catch {
      return "light";
    }
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* storage unavailable — keep theme in React state only */
    }
  }, [theme]);

  // ── Data loaders ───────────────────────────────────────────────────────────
  const loadTasks = useCallback(() => api.listTasks().then(setTasks).catch(() => { }), []);
  const loadRuns = useCallback(() => api.listRuns({ limit: 100 }).then(setRuns).catch(() => { }), []);
  const loadProviders = useCallback(() => api.listProviders().then(setProviders).catch(() => { }), []);
  const loadStats = useCallback(() => api.getStats().then(setStats).catch(() => { }), []);
  const loadCreds = useCallback(() => api.listCredentials().then(setCredentials).catch(() => { }), []);
  const loadSessions = useCallback(() => api.listSessions().then(setSessions).catch(() => { }), []);

  useEffect(() => {
    loadTasks();
    loadRuns();
    loadProviders();
    loadStats();
    loadCreds();
    loadSessions();
    api.getMode().then(setMode).catch(() => { });
    api.getConsoleConfig().then((c) => setConsoleAvailable(c.available)).catch(() => { });
  }, [loadTasks, loadRuns, loadProviders, loadStats, loadCreds, loadSessions]);

  // Poll the slowly-changing aggregates; live run state comes over the WS.
  useEffect(() => {
    const id = setInterval(() => {
      loadStats();
      loadProviders();
    }, 5000);
    return () => clearInterval(id);
  }, [loadStats, loadProviders]);

  // 1s clock for countdowns / elapsed timers.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // ── Merge fetched runs with live WS updates ─────────────────────────────────
  const mergedRuns = useMemo(() => {
    const byId = new Map<number, Run>();
    for (const r of runs) byId.set(r.id, r);
    for (const r of Object.values(liveRuns)) byId.set(r.id, r);
    return Array.from(byId.values()).sort((a, b) => b.id - a.id);
  }, [runs, liveRuns]);

  const latestRunByTask = useMemo(() => {
    const map: Record<number, Run> = {};
    for (const r of mergedRuns) if (!map[r.task_id] || r.id > map[r.task_id].id) map[r.task_id] = r;
    return map;
  }, [mergedRuns]);

  const activeRuns = mergedRuns.filter((r) => ["queued", "planning", "running"].includes(r.status)).length;
  const selectedRun = selectedRunId != null ? mergedRuns.find((r) => r.id === selectedRunId) ?? null : null;
  const taskById = useMemo(() => Object.fromEntries(tasks.map((t) => [t.id, t])), [tasks]);

  // ── Actions ──────────────────────────────────────────────────────────────--
  const handleRun = async (task: Task) => {
    setBusyTaskId(task.id);
    try {
      const run = await api.runTask(task.id);
      setRuns((prev) => [run, ...prev]);
      setSelectedRunId(run.id);
      setView("tasks");
      setTasksTab("live");
    } finally {
      setBusyTaskId(null);
    }
  };

  const handleSave = async (input: TaskInput, id?: number) => {
    if (id != null) await api.updateTask(id, input);
    else await api.createTask(input);
    await loadTasks();
  };

  const handleDelete = async (task: Task) => {
    if (!confirm(`Delete "${task.name}"?`)) return;
    await api.deleteTask(task.id);
    await loadTasks();
  };

  const handleToggle = async (task: Task) => {
    await api.updateTask(task.id, { enabled: !task.enabled });
    await loadTasks();
  };

  const handleRequeue = async (run: Run) => {
    const fresh = await api.requeueRun(run.id);
    setRuns((prev) => [fresh, ...prev]);
    setSelectedRunId(fresh.id);
  };

  const handleCancel = async (run: Run) => {
    await api.cancelRun(run.id).catch(() => { });
  };

  const handleAddCredential = async (provider: string, apiKey: string, label?: string | null) => {
    await api.createCredential(provider, apiKey, label);
    await loadCreds();
  };

  const sparkData = useMemo(() => runsPerDay(mergedRuns), [mergedRuns]);

  // ── Render ───────────────────────────────────────────────────────────────--
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <Sidebar view={view} onChange={setView} wsStatus={wsStatus} activeRuns={activeRuns} />

      <main className="flex-1 overflow-x-hidden px-4 pb-24 pt-5 md:px-8 md:pb-8">
        {/* Header */}
        <div className="mb-5 flex items-center justify-between gap-2">
          <div>
            <span className="md:hidden"><Logo size={20} /></span>
            <p className="hidden font-mono text-xs uppercase tracking-widest text-muted md:block">
              control plane · {view === "tasks" ? tasksTab : view}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              title={theme === "dark" ? "Switch to light" : "Switch to dark"}
              aria-label="Toggle theme"
              className="px-2.5"
              icon={theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowOnboarding(true)}
              icon={<Settings2 size={14} />}
            >
              <span className="hidden sm:inline">Providers</span>
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => { setEditingTask(null); setShowForm(true); }}
              icon={<Plus size={14} />}
            >
              New task
            </Button>
          </div>
        </div>

        <div className="mb-6">
          <StatsBar stats={stats} sparkData={sparkData} />
        </div>

        {/* Views */}
        {view === "tasks" && (
          <>
            {/* Tasks + Live runs share this pane; sub-tabs keep mobile to 3 nav items. */}
            <Tabs
              className="mb-4"
              tabs={[
                { id: "tasks", label: "Tasks" },
                { id: "live", label: activeRuns > 0 ? `Live · ${activeRuns}` : "Live" },
              ] as const}
              active={tasksTab}
              onChange={setTasksTab}
            />

            {tasksTab === "tasks" && (
              <TaskList
                tasks={tasks}
                latestRunByTask={latestRunByTask}
                now={now}
                busyTaskId={busyTaskId}
                onRun={handleRun}
                onEdit={(t) => { setEditingTask(t); setShowForm(true); }}
                onDelete={handleDelete}
                onToggle={handleToggle}
                onOpenRun={(id) => { setSelectedRunId(id); setTasksTab("live"); }}
              />
            )}

            {tasksTab === "live" && (
              <div className="stagger space-y-2">
                {mergedRuns.length === 0 && (
                  <div className="rounded-lg border border-dashed border-edge p-8 text-center text-muted">
                    No runs yet. Hit “Run now” on a task.
                  </div>
                )}
                {mergedRuns.slice(0, 40).map((r) => {
                  const meta = STATUS_META[r.status];
                  return (
                    <button
                      key={r.id}
                      onClick={() => setSelectedRunId(r.id)}
                      className="flex w-full items-center gap-3 rounded-lg border border-edge bg-panel/60 px-3 py-2.5 text-left hover:border-amber/40"
                    >
                      <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot}`} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-mono text-sm text-ink">
                          {taskById[r.task_id]?.name ?? `Task ${r.task_id}`}
                        </span>
                        <span className="font-mono text-[11px] text-muted">
                          run #{r.id} · {r.provider ?? "—"} · {fmtTime(r.created_at)}
                        </span>
                      </span>
                      <span className={`shrink-0 font-mono text-xs ${meta.text}`}>{meta.label}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </>
        )}

        {view === "build" && (
          <SessionView
            sessions={sessions}
            selectedId={selectedSessionId}
            onSelect={setSelectedSessionId}
            onSessionsChanged={loadSessions}
            providers={providers}
          />
        )}

        {view === "providers" && (
          <ProvidersPanel
            providers={providers}
            now={now}
            onRefresh={loadProviders}
            consoleAvailable={consoleAvailable}
            consoleToken={consoleToken}
            onSetConsoleToken={setConsoleToken}
          />
        )}
      </main>

      {/* Run viewer — right drawer on desktop, full-screen on mobile */}
      {selectedRun && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/50 md:bg-transparent">
          <div
            className="absolute inset-0 hidden md:block"
            onClick={() => setSelectedRunId(null)}
          />
          <div className="relative z-10 h-full w-full overflow-hidden p-0 md:w-[520px] md:p-3">
            <RunViewer
              run={selectedRun}
              taskName={taskById[selectedRun.task_id]?.name}
              now={now}
              onRequeue={handleRequeue}
              onCancel={handleCancel}
              onClose={() => setSelectedRunId(null)}
            />
          </div>
        </div>
      )}

      {showForm && (
        <TaskForm
          task={editingTask}
          providers={providers}
          onSave={handleSave}
          onClose={() => setShowForm(false)}
        />
      )}

      {showOnboarding && (
        <Onboarding
          mode={mode}
          providers={providers}
          credentials={credentials}
          onAddCredential={handleAddCredential}
          onClose={() => setShowOnboarding(false)}
        />
      )}
    </div>
  );
}
