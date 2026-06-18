// SettingsPanel.tsx — D-0027 / D-0023(c). Settings = the config home.
// Sections: AI Plans (providers, renamed for non-coders), Console/auth, Budget (placeholder).
// Mobile: stacked cards (no sidebar). Desktop: same stack, wider grid for provider cards.
// Wraps ProvidersPanel + SecretsPanel; adds the telemetry-consent placeholder (D-0022 level 2).
// D-0027 fix 2: all sections collapsed by default (accordion).
import { type ReactNode, useState } from "react";
import { ChevronDown, ChevronRight, KeyRound, Server, ShieldOff, Sliders } from "lucide-react";
import type { Credential, Mode, ProviderHealth, UsageSummary } from "../types";
import ProvidersPanel from "./ProvidersPanel";
import SecretsPanel from "./SecretsPanel";
import { Badge, Card } from "../ui";

// ─── Collapsible section shell ────────────────────────────────────────────────
function Section({
  icon,
  title,
  sub,
  defaultOpen = false,
  children,
}: {
  icon: ReactNode;
  title: string;
  sub?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-xl border border-edge overflow-hidden">
      {/* Header — full-width tap target (mobile-friendly) */}
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        className="flex w-full items-center gap-3 px-4 py-3.5 text-left hover:bg-brand/5 transition-colors"
      >
        <span className="shrink-0 text-brand">{icon}</span>
        <div className="flex-1 min-w-0">
          <span className="block font-mono text-sm font-semibold text-ink">{title}</span>
          {sub && <span className="block text-[11px] text-muted">{sub}</span>}
        </div>
        <span className="shrink-0 text-muted">
          {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        </span>
      </button>

      {open && (
        <div className="border-t border-edge px-4 pb-4 pt-4">
          {children}
        </div>
      )}
    </div>
  );
}

// ─── Telemetry consent placeholder (D-0022 level 2, not yet wired) ──────────
function TelemetryContent() {
  const [allowed, setAllowed] = useState(false);
  return (
    <Card className="px-4 py-3">
      <label className="flex items-start gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={allowed}
          onChange={(e) => setAllowed(e.target.checked)}
          className="mt-0.5 shrink-0 accent-brand"
        />
        <span className="text-sm text-ink">
          Send anonymous usage counts to help improve batonkeep
          <span className="mt-1 block text-[11px] text-muted">
            No prompts, file names, content, or identifiers are ever collected.
            Only structural metadata: feature flags, error classes, run counts.
            You can revoke this at any time.
          </span>
        </span>
      </label>
      <div className="mt-2 flex items-center gap-1.5">
        <Badge tone="neutral">coming soon</Badge>
        <span className="text-[11px] text-muted">live collection not yet active</span>
      </div>
    </Card>
  );
}

// ─── Budget placeholder ───────────────────────────────────────────────────────
function BudgetContent() {
  return (
    <Card className="px-4 py-3">
      <p className="text-sm text-muted">
        Set <code className="font-mono text-ink">DAILY_BUDGET_USD</code> in your deployment
        environment to cap daily API spend. When the cap is hit, new runs degrade to free
        plan-CLI providers automatically. Budget controls in this UI are coming soon.
      </p>
    </Card>
  );
}

// ─── Main export ──────────────────────────────────────────────────────────────
interface Props {
  providers: ProviderHealth[];
  credentials: Credential[];
  usage: UsageSummary | null;
  mode: Mode | null;
  now: number;
  onRefresh: () => void;
  consoleAvailable: boolean;
  consoleToken: string;
  onSetConsoleToken: (t: string) => void;
  appAuthEnabled: boolean;
}

export default function SettingsPanel({
  providers,
  now,
  usage,
  onRefresh,
  consoleAvailable,
  consoleToken,
  onSetConsoleToken,
  appAuthEnabled,
}: Props) {
  return (
    <div className="space-y-3">
      {/* ── AI Plans ─────────────────────────────────────────────────────── */}
      <Section
        icon={<Server size={16} />}
        title="AI Plans"
        sub="Connected AI providers — health, headroom, and re-auth"
        defaultOpen={false}
      >
        <ProvidersPanel
          providers={providers}
          now={now}
          usage={usage}
          onRefresh={onRefresh}
          consoleAvailable={consoleAvailable}
          consoleToken={consoleToken}
          onSetConsoleToken={onSetConsoleToken}
          appAuthEnabled={appAuthEnabled}
        />
      </Section>

      {/* ── Secrets / credentials ────────────────────────────────────────── */}
      <Section
        icon={<KeyRound size={16} />}
        title="API credentials"
        sub="Stored keys for API-mode providers"
        defaultOpen={false}
      >
        <SecretsPanel />
      </Section>

      {/* ── Budget ───────────────────────────────────────────────────────── */}
      <Section
        icon={<Sliders size={16} />}
        title="Budget & cost"
        sub="Daily spend cap and over-budget policy"
        defaultOpen={false}
      >
        <BudgetContent />
      </Section>

      {/* ── Telemetry consent ────────────────────────────────────────────── */}
      <Section
        icon={<ShieldOff size={16} />}
        title="Diagnostic telemetry"
        sub="Anonymous, content-free product diagnostics — default off"
        defaultOpen={false}
      >
        <TelemetryContent />
      </Section>
    </div>
  );
}
