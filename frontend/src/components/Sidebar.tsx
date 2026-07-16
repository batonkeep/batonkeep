// Sidebar.tsx — primary nav. Left rail on desktop (with sub-labels), bottom
// tab bar on mobile (5 items, same tap targets). D-0027: Providers → Settings,
// Cockpit → Analytics; sub-labels on desktop; human labels; mobile unchanged.
import { FolderKanban, Gauge, Hammer, ListChecks, Settings2 } from "lucide-react";
import Logo from "../ui/Logo";
import VersionBadge from "./VersionBadge";
import type { WsStatus } from "../useLiveFeed";

// "Live" (runs) is folded into the Tasks pane as a sub-tab; Providers folded into
// Settings (D-0027 / D-0023). Projects (S0 substrate) brings the bottom nav to
// five items on mobile — the platform ceiling.
export type View = "tasks" | "build" | "projects" | "settings" | "cockpit";

interface Props {
  view: View;
  onChange: (v: View) => void;
  wsStatus: WsStatus;
  activeRuns: number;
  // Mobile: hide the bottom tab bar so an open build session gets the full
  // screen. Desktop (md+) always shows the rail.
  immersive?: boolean;
}

const ITEMS: {
  id: View;
  label: string;
  sub: string; // desktop sub-label
  icon: typeof ListChecks;
}[] = [
  { id: "tasks",    label: "Tasks",     sub: "schedule + run",     icon: ListChecks },
  { id: "build",    label: "Build",     sub: "sessions + publish",  icon: Hammer },
  { id: "projects", label: "Projects",  sub: "work + context",      icon: FolderKanban },
  { id: "cockpit",  label: "Analytics", sub: "ops telemetry",       icon: Gauge },
  { id: "settings", label: "Settings",  sub: "AI plans + config",   icon: Settings2 },
];

export default function Sidebar({ view, onChange, wsStatus, activeRuns, immersive = false }: Props) {
  return (
    <nav
      className={`
        ${immersive ? "hidden" : "flex"} md:flex
        fixed bottom-0 left-0 right-0 z-30 flex-row justify-around border-t border-edge bg-panel/95 backdrop-blur
        md:static md:h-full md:w-56 md:flex-col md:justify-start md:gap-1 md:border-r md:border-t-0 md:p-3
      `}
    >
      {/* Brand — desktop only */}
      <div className="hidden md:mb-5 md:flex md:items-center md:px-2 md:pt-1">
        <Logo size={30} />
      </div>

      {ITEMS.map(({ id, label, sub, icon: Icon }) => {
        const active = view === id;
        return (
          <button
            key={id}
            onClick={() => onChange(id)}
            className={`
              relative flex flex-1 flex-col items-center gap-1 py-2.5 text-[11px]
              md:flex-none md:flex-row md:gap-3 md:rounded-lg md:px-3 md:py-2.5 md:text-sm
              ${active ? "text-brand md:bg-brand/10" : "text-muted hover:text-ink"}
              transition-colors
            `}
          >
            <Icon size={18} className={active ? "text-brand" : ""} />
            {/* Mobile: just the main label. Desktop: label + sub-label stacked. */}
            <span className="flex flex-col items-center md:items-start">
              <span className="font-mono">{label}</span>
              <span className="hidden md:inline text-[10px] text-muted font-sans">{sub}</span>
            </span>
            {id === "tasks" && activeRuns > 0 && (
              <span className="absolute right-1/4 top-1 h-1.5 w-1.5 rounded-full bg-live animate-pulse-live md:static md:ml-auto md:h-auto md:w-auto md:rounded-none md:bg-transparent md:text-xs md:text-live">
                <span className="hidden md:inline">{activeRuns}</span>
              </span>
            )}
          </button>
        );
      })}

      {/* Connection signal — desktop only, more prominent (D-0027 fix 3) */}
      <div className="hidden md:mt-auto md:flex md:items-center md:gap-2 md:px-3 md:py-2">
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${
            wsStatus === "open"
              ? "bg-live shadow-[0_0_6px_2px_rgba(34,197,94,0.4)]"
              : wsStatus === "connecting"
              ? "bg-defer animate-pulse-live"
              : "bg-bad"
          }`}
        />
        <span className={`font-mono text-xs ${
          wsStatus === "open" ? "text-live" : wsStatus === "connecting" ? "text-defer" : "text-bad"
        }`}>
          {wsStatus === "open" ? "connected" : wsStatus === "connecting" ? "connecting…" : "offline"}
        </span>
      </div>

      {/* Running version + update hint — desktop only (D-0053) */}
      <VersionBadge compact />
    </nav>
  );
}
