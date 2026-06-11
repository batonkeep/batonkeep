// App.tsx — top-level shell: data orchestration, view routing, modals.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LogOut, Moon, Plus, Search, Sun } from "lucide-react";
import { api } from "./api";
import { useLiveFeed } from "./useLiveFeed";
import type { Credential, Mode, ProviderHealth, Run, Session, Stats, Task, TaskInput, TaskTemplate, UsageSummary } from "./types";
import Sidebar, { View } from "./components/Sidebar";
import StatsBar from "./components/StatsBar";
import TaskList from "./components/TaskList";
import TaskForm from "./components/TaskForm";
import RunViewer from "./components/RunViewer";
import SessionView from "./components/SessionView";
import SettingsPanel from "./components/SettingsPanel";
import CockpitPanel from "./components/CockpitPanel";
import Onboarding from "./components/Onboarding";
import LoginPage from "./components/LoginPage";
import Styleguide from "./components/Styleguide";
import { Button, Input, Logo, Select, Tabs } from "./ui";
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

  return <AuthGate />;
}

// App-level auth gate (D-0023). Resolves the backend's auth status once: if
// app-auth is on and we have no valid session, show the login page; otherwise
// render the app. `appAuthEnabled` flows down so the providers console folds the
// legacy token into the session (no separate token entry when auth is on).
function AuthGate() {
  const [status, setStatus] = useState<{ enabled: boolean; authed: boolean } | null>(null);

  const refresh = useCallback(() => {
    api
      .getAuthStatus()
      .then((s) => setStatus({ enabled: s.auth_enabled, authed: s.authenticated }))
      // On a network/whatever failure, fail open to the app (it will surface its
      // own load errors) rather than trapping the user on a blank gate.
      .catch(() => setStatus({ enabled: false, authed: true }));
  }, []);
  useEffect(refresh, [refresh]);

  if (status === null) return null; // brief pre-resolve; avoids a login flash
  if (status.enabled && !status.authed) return <LoginPage onAuthed={refresh} />;
  return <AppShell appAuthEnabled={status.enabled} onLogout={refresh} />;
}

function AppShell({ appAuthEnabled, onLogout }: { appAuthEnabled: boolean; onLogout: () => void }) {
  const { status: wsStatus, liveRuns } = useLiveFeed();
  const [view, setView] = useState<View>("tasks");
  const [tasksTab, setTasksTab] = useState<"tasks" | "live">("tasks");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
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
  // Preset to pre-fill a NEW task when started from a starter template.
  const [formInitial, setFormInitial] = useState<TaskInput | null>(null);
  const [taskTemplates, setTaskTemplates] = useState<TaskTemplate[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [runQuery, setRunQuery] = useState(""); // filter the Live runs list
  const [runStatusFilter, setRunStatusFilter] = useState<string>("all");
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null);
  // “+ New” popover (D-0027 item 5 / D-0024). Ref for outside-click close.
  const [showNewMenu, setShowNewMenu] = useState(false);
  const newMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!showNewMenu) return;
    const handler = (e: MouseEvent) => {
      if (newMenuRef.current && !newMenuRef.current.contains(e.target as Node)) {
        setShowNewMenu(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showNewMenu]);

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
  const loadStats = useCallback(() => {
    api.getStats().then(setStats).catch(() => { });
    api.getUsage().then(setUsage).catch(() => { });
  }, []);
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
    api.listTaskTemplates().then(setTaskTemplates).catch(() => { });
  }, [loadTasks, loadRuns, loadProviders, loadStats, loadCreds, loadSessions]);

  // Poll the slowly-changing aggregates. Run state is driven live over the WS, but
  // we also re-fetch runs here as a safety net: a single dropped run.update frame
  // (e.g. the terminal "succeeded" lost under a heavy token-stream) would otherwise
  // strand a finished run showing "running" until a manual reload. Polling lets the
  // status self-heal within the interval.
  useEffect(() => {
    const id = setInterval(() => {
      loadStats();
      loadProviders();
      loadRuns();
    }, 5000);
    return () => clearInterval(id);
  }, [loadStats, loadProviders, loadRuns]);

  // 1s clock for countdowns / elapsed timers.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // ── Merge fetched runs with live WS updates ─────────────────────────────────
  // Live WS state is normally freshest, so it wins — EXCEPT when the fetched run is
  // already terminal but the live one isn't. That happens when the terminal
  // run.update frame was dropped: the REST poll then carries the true final state,
  // which must not be clobbered by the stale "running" still sitting in liveRuns.
  const TERMINAL = ["succeeded", "failed", "cancelled"];
  const mergedRuns = useMemo(() => {
    const byId = new Map<number, Run>();
    for (const r of runs) byId.set(r.id, r);
    for (const r of Object.values(liveRuns)) {
      const fetched = byId.get(r.id);
      if (fetched && TERMINAL.includes(fetched.status) && !TERMINAL.includes(r.status)) continue;
      byId.set(r.id, r);
    }
    return Array.from(byId.values()).sort((a, b) => b.id - a.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, liveRuns]);

  const latestRunByTask = useMemo(() => {
    const map: Record<number, Run> = {};
    for (const r of mergedRuns) if (!map[r.task_id] || r.id > map[r.task_id].id) map[r.task_id] = r;
    return map;
  }, [mergedRuns]);

  const activeRuns = mergedRuns.filter((r) => ["queued", "planning", "running"].includes(r.status)).length;
  // Immersive build session (mobile): once a session is open, give it the full
  // screen — collapse the header to the brand and drop the bottom tab bar (the
  // in-session "Sessions" back button is the way out). Desktop ignores this.
  const immersive = view === "build" && selectedSessionId != null;
  const selectedRun = selectedRunId != null ? mergedRuns.find((r) => r.id === selectedRunId) ?? null : null;
  const taskById = useMemo(() => Object.fromEntries(tasks.map((t) => [t.id, t])), [tasks]);

  // Statuses actually present, so the run filter only offers real values.
  const runStatuses = useMemo(() => {
    const seen = new Set<string>();
    for (const r of mergedRuns) seen.add(r.status);
    return Array.from(seen);
  }, [mergedRuns]);

  // Live runs filtered by status + a search over task name / provider / run #.
  const filteredRuns = useMemo(() => {
    const q = runQuery.trim().toLowerCase();
    return mergedRuns.filter((r) => {
      if (runStatusFilter !== "all" && r.status !== runStatusFilter) return false;
      if (!q) return true;
      const name = (taskById[r.task_id]?.name ?? `task ${r.task_id}`).toLowerCase();
      return (
        name.includes(q) ||
        (r.provider ?? "").toLowerCase().includes(q) ||
        String(r.id).includes(q)
      );
    });
  }, [mergedRuns, taskById, runQuery, runStatusFilter]);

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
  // Human-readable page title map (D-0027 item 5).
  const VIEW_TITLES: Record<View, string> = {
    tasks: "Tasks",
    build: "Build",
    settings: "Settings",
    cockpit: "Analytics",
  };

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <Sidebar view={view} onChange={setView} wsStatus={wsStatus} activeRuns={activeRuns} immersive={immersive} />

      <main
        className={`flex-1 overflow-x-hidden px-4 pt-5 md:px-8 md:pb-8 ${
          immersive ? "pb-0" : "pb-24"
        }`}
      >
        {/* Header. Mobile: brand logo only (immersive mode collapses further).
            Desktop: human page title (D-0027 item 5) + action buttons. */}
        <div className="mb-5 flex items-center justify-between gap-2">
          <div>
            {/* Mobile: show logo */}
            <span className="md:hidden"><Logo size={30} /></span>
            {/* Desktop: human-readable page title */}
            <h1 className="hidden font-mono text-base font-semibold text-ink md:block">
              {VIEW_TITLES[view === "tasks" ? "tasks" : view]}
            </h1>
          </div>

          <div className={`items-center gap-2 ${immersive ? "hidden md:flex" : "flex"}`}>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              title={theme === "dark" ? "Switch to light" : "Switch to dark"}
              aria-label="Toggle theme"
              className="px-2.5"
              icon={theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            />
            {appAuthEnabled && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => { api.logout().finally(onLogout); }}
                title="Sign out"
                aria-label="Sign out"
                className="px-2.5"
                icon={<LogOut size={14} />}
              />
            )}
            {/* + New popover (D-0027 item 5 / D-0024): New Task | New Build Session */}
            <div className="relative" ref={newMenuRef}>
              <Button
                variant="primary"
                size="sm"
                onClick={() => setShowNewMenu((s) => !s)}
                icon={<Plus size={14} />}
              >
                New
              </Button>
              {showNewMenu && (
                <div className="absolute right-0 top-full z-50 mt-1.5 min-w-[180px] rounded-xl border border-edge bg-panel shadow-lg">
                  <button
                    className="flex w-full items-center gap-2.5 px-4 py-3 text-left text-sm hover:bg-brand/5"
                    onClick={() => {
                      setShowNewMenu(false);
                      setFormInitial(null);
                      setEditingTask(null);
                      setShowForm(true);
                    }}
                  >
                    <span className="font-mono text-brand">+</span>
                    <span>
                      <span className="block font-semibold text-ink">New task</span>
                      <span className="text-[11px] text-muted">Schedule + automate</span>
                    </span>
                  </button>
                  <div className="mx-4 border-t border-edge" />
                  <button
                    className="flex w-full items-center gap-2.5 px-4 py-3 text-left text-sm hover:bg-brand/5"
                    onClick={() => {
                      setShowNewMenu(false);
                      setView("build");
                    }}
                  >
                    <span className="font-mono text-brand">▶</span>
                    <span>
                      <span className="block font-semibold text-ink">New build session</span>
                      <span className="text-[11px] text-muted">Chat + publish</span>
                    </span>
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Run/task aggregates — irrelevant to Build, and the Cockpit is their
            consolidated superset, so hide the strip on both. */}
        {view !== "build" && view !== "cockpit" && view !== "settings" && (
          <div className="mb-6">
            <StatsBar stats={stats} usage={usage} sparkData={sparkData} />
          </div>
        )}

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
                onEdit={(t) => { setFormInitial(null); setEditingTask(t); setShowForm(true); }}
                onDelete={handleDelete}
                onToggle={handleToggle}
                onOpenRun={(id) => { setSelectedRunId(id); setTasksTab("live"); }}
                onNewTask={() => { setFormInitial(null); setEditingTask(null); setShowForm(true); }}
                templates={taskTemplates}
                onUseTemplate={(input) => { setEditingTask(null); setFormInitial(input); setShowForm(true); }}
              />
            )}

            {tasksTab === "live" && (
              <div className="stagger space-y-2">
                {mergedRuns.length === 0 && (
                  <div className="rounded-xl border border-dashed border-edge p-8 text-center">
                    <p className="mb-1 font-mono text-sm font-semibold text-ink">No runs yet</p>
                    <p className="text-xs text-muted">Hit “Run now” on a task to see live output here.</p>
                  </div>
                )}
                {mergedRuns.length > 0 && (
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                    <div className="relative flex-1">
                      <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
                      <Input
                        value={runQuery}
                        onChange={(e) => setRunQuery(e.target.value)}
                        placeholder="Search runs — task, provider, or #…"
                        className="pl-9"
                        aria-label="Search runs"
                      />
                    </div>
                    <Select
                      value={runStatusFilter}
                      onChange={(e) => setRunStatusFilter(e.target.value)}
                      className="h-10 sm:w-44"
                      aria-label="Filter runs by status"
                    >
                      <option value="all">All statuses</option>
                      {runStatuses.map((s) => (
                        <option key={s} value={s}>
                          {STATUS_META[s as keyof typeof STATUS_META]?.label ?? s}
                        </option>
                      ))}
                    </Select>
                  </div>
                )}
                {mergedRuns.length > 0 && filteredRuns.length === 0 && (
                  <div className="rounded-xl border border-dashed border-edge p-8 text-center text-xs text-muted">
                    No runs match your filters.
                  </div>
                )}
                {filteredRuns.slice(0, 40).map((r) => {
                  const meta = STATUS_META[r.status];
                  return (
                    <button
                      key={r.id}
                      onClick={() => setSelectedRunId(r.id)}
                      className="flex w-full items-center gap-3 rounded-lg border border-edge bg-panel/60 px-3 py-2.5 text-left hover:border-brand/40"
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
            consoleAvailable={consoleAvailable}
            consoleToken={consoleToken}
            appAuthEnabled={appAuthEnabled}
          />
        )}

        {view === "settings" && (
          <SettingsPanel
            providers={providers}
            credentials={credentials}
            mode={mode}
            now={now}
            onRefresh={loadProviders}
            consoleAvailable={consoleAvailable}
            consoleToken={consoleToken}
            onSetConsoleToken={setConsoleToken}
            appAuthEnabled={appAuthEnabled}
          />
        )}

        {view === "cockpit" && <CockpitPanel />}
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
          initial={formInitial}
          providers={providers}
          onSave={handleSave}
          onClose={() => { setShowForm(false); setFormInitial(null); }}
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
