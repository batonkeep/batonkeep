// Sidebar.tsx — primary nav. Left rail on desktop, bottom tab bar on mobile.
import { Hammer, ListChecks, Radio, Server } from "lucide-react";
import Logo from "../ui/Logo";
import type { WsStatus } from "../useLiveFeed";

export type View = "tasks" | "live" | "build" | "providers";

interface Props {
  view: View;
  onChange: (v: View) => void;
  wsStatus: WsStatus;
  activeRuns: number;
}

const ITEMS: { id: View; label: string; icon: typeof ListChecks }[] = [
  { id: "tasks", label: "Tasks", icon: ListChecks },
  { id: "live", label: "Live", icon: Radio },
  { id: "build", label: "Build", icon: Hammer },
  { id: "providers", label: "Providers", icon: Server },
];

export default function Sidebar({ view, onChange, wsStatus, activeRuns }: Props) {
  return (
    <nav
      className="
        fixed bottom-0 left-0 right-0 z-30 flex flex-row justify-around border-t border-edge bg-panel/95 backdrop-blur
        md:static md:h-full md:w-56 md:flex-col md:justify-start md:gap-1 md:border-r md:border-t-0 md:p-3
      "
    >
      {/* Brand — desktop only */}
      <div className="hidden md:mb-5 md:flex md:items-center md:px-2 md:pt-1">
        <Logo size={20} />
      </div>

      {ITEMS.map(({ id, label, icon: Icon }) => {
        const active = view === id;
        return (
          <button
            key={id}
            onClick={() => onChange(id)}
            className={`
              relative flex flex-1 flex-col items-center gap-1 py-2.5 text-[11px]
              md:flex-none md:flex-row md:gap-3 md:rounded-lg md:px-3 md:py-2.5 md:text-sm
              ${active ? "text-amber md:bg-amber/10" : "text-muted hover:text-ink"}
              transition-colors
            `}
          >
            <Icon size={18} className={active ? "text-amber" : ""} />
            <span className="font-mono">{label}</span>
            {id === "live" && activeRuns > 0 && (
              <span className="absolute right-1/4 top-1 h-1.5 w-1.5 rounded-full bg-live animate-pulse-live md:static md:ml-auto md:h-auto md:w-auto md:rounded-none md:bg-transparent md:text-xs md:text-live">
                <span className="hidden md:inline">{activeRuns}</span>
              </span>
            )}
          </button>
        );
      })}

      {/* Connection status — desktop only */}
      <div className="hidden md:mt-auto md:flex md:items-center md:gap-2 md:px-3 md:py-2 md:text-xs">
        <span
          className={`h-2 w-2 rounded-full ${
            wsStatus === "open" ? "bg-live" : wsStatus === "connecting" ? "bg-defer animate-pulse-live" : "bg-bad"
          }`}
        />
        <span className="font-mono text-muted">{wsStatus === "open" ? "live feed" : wsStatus}</span>
      </div>
    </nav>
  );
}
