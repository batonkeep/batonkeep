// Styleguide.tsx — living reference for the Batonkeep design system (D-track).
// Open via #styleguide. Shows brand, tokens, and every ui/ primitive so new
// surfaces (M1–M6) compose from a known palette instead of inventing one.
import { Plus } from "lucide-react";
import { Badge, Button, Card, Logo, LogoMark, StatusDot, type Tone } from "../ui";

const SWATCHES: { name: string; cls: string; note: string }[] = [
  { name: "base", cls: "bg-base", note: "page background" },
  { name: "panel", cls: "bg-panel", note: "raised surface" },
  { name: "edge", cls: "bg-edge", note: "hairline borders" },
  { name: "ink", cls: "bg-ink", note: "primary text" },
  { name: "muted", cls: "bg-muted", note: "dimmed text" },
  { name: "amber", cls: "bg-amber", note: "signal accent" },
  { name: "live", cls: "bg-live", note: "live / streaming only" },
  { name: "ok", cls: "bg-ok", note: "success" },
  { name: "warn", cls: "bg-warn", note: "warning" },
  { name: "bad", cls: "bg-bad", note: "error" },
  { name: "defer", cls: "bg-defer", note: "deferred" },
];

const TONES: Tone[] = ["neutral", "amber", "ok", "warn", "bad", "live", "defer"];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-10">
      <h2 className="mb-3 font-mono text-xs font-semibold uppercase tracking-widest text-muted">{title}</h2>
      {children}
    </section>
  );
}

export default function Styleguide() {
  return (
    <div className="mx-auto max-w-4xl px-5 py-8">
      <header className="mb-10 flex items-center justify-between">
        <Logo size={26} />
        <Badge tone="amber">design system · D-track</Badge>
      </header>

      <Section title="Brand mark">
        <Card className="flex flex-wrap items-center gap-8 p-6">
          <LogoMark size={56} className="text-amber" />
          <Logo size={28} />
          <Logo wordmark={false} size={32} />
          <div className="font-mono text-xs text-muted">
            relay baton mid-hand-off — work passed between agents,<br />failover across owned plans
          </div>
        </Card>
      </Section>

      <Section title="Color tokens">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {SWATCHES.map((s) => (
            <Card key={s.name} className="overflow-hidden">
              <div className={`h-12 w-full ${s.cls}`} />
              <div className="px-3 py-2">
                <div className="font-mono text-xs font-semibold text-ink">{s.name}</div>
                <div className="text-[11px] text-muted">{s.note}</div>
              </div>
            </Card>
          ))}
        </div>
      </Section>

      <Section title="Typography">
        <Card className="space-y-2 p-5">
          <p className="font-mono text-2xl font-semibold tracking-tight text-ink">IBM Plex Mono — headings & metrics</p>
          <p className="font-sans text-base text-ink">IBM Plex Sans — body copy and longer-form reading.</p>
          <p className="font-mono text-sm text-muted">0123456789 · failover · cooldown · owner_id</p>
        </Card>
      </Section>

      <Section title="Buttons">
        <Card className="flex flex-wrap items-center gap-3 p-5">
          <Button variant="primary" icon={<Plus size={15} />}>Primary</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="danger">Danger</Button>
          <Button variant="primary" size="sm">Small</Button>
          <Button variant="outline" disabled>Disabled</Button>
        </Card>
      </Section>

      <Section title="Badges & status">
        <Card className="flex flex-wrap items-center gap-2 p-5">
          {TONES.map((t) => (
            <Badge key={t} tone={t}>{t}</Badge>
          ))}
        </Card>
        <Card className="mt-3 flex flex-wrap items-center gap-5 p-5">
          {TONES.map((t) => (
            <span key={t} className="inline-flex items-center gap-2 text-xs text-muted">
              <StatusDot tone={t} pulse={t === "live"} /> {t}
            </span>
          ))}
        </Card>
      </Section>

      <Section title="Cards">
        <div className="grid gap-3 sm:grid-cols-2">
          <Card className="p-5"><div className="text-sm text-ink">Default card</div><div className="text-xs text-muted">raised surface over the textured base</div></Card>
          <Card active className="p-5"><div className="text-sm text-ink">Active card</div><div className="text-xs text-muted">amber hairline + glow</div></Card>
        </div>
      </Section>

      <footer className="mt-12 border-t border-edge pt-4 font-mono text-[11px] text-muted">
        Batonkeep design system — D-0006. Compose new surfaces from <code className="text-amber">src/ui/</code>.
      </footer>
    </div>
  );
}
