// Styleguide.tsx — living reference for the Batonkeep design system (D-track).
// Open via #styleguide. Shows brand, tokens, and every ui/ primitive so new
// surfaces (M1–M6) compose from a known palette instead of inventing one.
import { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { Badge, Button, Card, Field, Input, Logo, LogoMark, Modal, Select, ShieldMark, ShieldSolid, StatusDot, Tabs, type Tone } from "../ui";

const ACCENTS = [
  { id: "amber", label: "Amber", note: "current · mission-control" },
  { id: "indigo", label: "Indigo", note: "professional · distinct from live-cyan" },
  { id: "blue", label: "Blue", note: "trust · close to live-cyan" },
  { id: "red", label: "Red", note: "#E74C3C · warning: ≈ the error token" },
] as const;

function setAccent(id: string) {
  if (id === "amber") document.documentElement.removeAttribute("data-accent");
  else document.documentElement.setAttribute("data-accent", id);
}

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

const RUN_TABS = [
  { id: "report", label: "Report" },
  { id: "json", label: "JSON" },
  { id: "raw", label: "Raw" },
] as const;

export default function Styleguide() {
  const [tab, setTab] = useState<(typeof RUN_TABS)[number]["id"]>("report");
  const [modalOpen, setModalOpen] = useState(false);
  const [accent, setAccentId] = useState("indigo");
  // Open the lab on indigo (recommended) so the solid-shield brand shows in context;
  // restore amber on unmount so the rest of the app is unaffected.
  useEffect(() => {
    setAccent("indigo");
    return () => setAccent("amber");
  }, []);

  return (
    <div className="mx-auto max-w-4xl px-5 py-8">
      <header className="mb-10 flex items-center justify-between">
        <Logo size={26} />
        <Badge tone="amber">design system · D-track</Badge>
      </header>

      <Section title="Brand lab — mark + accent (live preview)">
        {/* Accent switcher — re-themes the whole page via data-accent on <html>. */}
        <Card className="mb-3 flex flex-wrap items-center gap-2 p-4">
          <span className="mr-1 font-mono text-[11px] uppercase tracking-wider text-muted">accent</span>
          {ACCENTS.map((a) => (
            <Button
              key={a.id}
              size="sm"
              variant={accent === a.id ? "primary" : "outline"}
              onClick={() => { setAccentId(a.id); setAccent(a.id); }}
              title={a.note}
            >
              {a.label}
            </Button>
          ))}
          <span className="ml-2 text-[11px] text-muted">{ACCENTS.find((a) => a.id === accent)?.note}</span>
        </Card>

        {/* Mark candidates — all use the accent token, so they follow the switch. */}
        <div className="grid gap-3 sm:grid-cols-4">
          <Card active className="flex flex-col items-center gap-3 p-6">
            <ShieldSolid size={52} />
            <div className="text-center font-mono text-[11px] text-muted">solid shield<br />(brand = primary)</div>
          </Card>
          <Card className="flex flex-col items-center gap-3 p-6">
            <ShieldMark size={52} className="text-ink" />
            <div className="text-center font-mono text-[11px] text-muted">outline shield<br />(baton + keep)</div>
          </Card>
          <Card className="flex flex-col items-center gap-3 p-6">
            <ShieldMark size={52} relay className="text-ink" />
            <div className="text-center font-mono text-[11px] text-muted">relay + shield<br />(both, busier)</div>
          </Card>
          <Card className="flex flex-col items-center gap-3 p-6">
            <LogoMark size={52} className="text-amber" />
            <div className="text-center font-mono text-[11px] text-muted">relay baton<br />(no shield)</div>
          </Card>
        </div>

        {/* Favicon-scale legibility check for the solid shield. */}
        <Card className="mt-3 flex items-center gap-5 p-5">
          <span className="font-mono text-[11px] uppercase tracking-wider text-muted">favicon scale</span>
          <ShieldSolid size={16} />
          <ShieldSolid size={20} />
          <ShieldSolid size={28} />
          <span className="text-[11px] text-muted">solid shapes stay legible where outlines collapse</span>
        </Card>

        {/* Wordmark lockups + a few accent surfaces so the switch is visible. */}
        <Card className="mt-3 flex flex-wrap items-center gap-6 p-6">
          <span className="inline-flex items-center gap-2.5">
            <ShieldSolid size={28} />
            <span className="font-mono text-lg font-semibold tracking-tight text-ink">
              Baton<span className="text-muted">keep</span>
            </span>
          </span>
          <Button variant="primary" size="sm">Primary</Button>
          <Badge tone="amber">accent badge</Badge>
          <StatusDot tone="amber" /> <span className="text-xs text-muted">accent</span>
          <StatusDot tone="live" pulse /> <span className="text-xs text-muted">live (cyan — reserved)</span>
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

      <Section title="Form controls">
        <Card className="grid gap-4 p-5 sm:grid-cols-2">
          <Field label="Task name" hint="shown in the task list">
            <Input placeholder="daily competitor watch" />
          </Field>
          <Field label="Strategy">
            <Select defaultValue="capability">
              <option value="capability">capability</option>
              <option value="fixed">fixed</option>
              <option value="round_robin">round_robin</option>
            </Select>
          </Field>
          <Field label="API key" hint="stored encrypted at rest">
            <Input type="password" placeholder="sk-…" className="font-mono" />
          </Field>
          <Field label="Disabled">
            <Input placeholder="read-only" disabled />
          </Field>
        </Card>
      </Section>

      <Section title="Tabs">
        <Card className="p-5">
          <Tabs tabs={RUN_TABS} active={tab} onChange={setTab} />
          <div className="mt-3 font-mono text-xs text-muted">active: {tab}</div>
        </Card>
      </Section>

      <Section title="Overlay">
        <Card className="p-5">
          <Button variant="outline" onClick={() => setModalOpen(true)}>Open modal</Button>
          <Modal
            open={modalOpen}
            onClose={() => setModalOpen(false)}
            title="Modal primitive"
            footer={
              <>
                <Button variant="ghost" onClick={() => setModalOpen(false)}>Cancel</Button>
                <Button variant="primary" onClick={() => setModalOpen(false)}>Confirm</Button>
              </>
            }
          >
            <p className="text-sm text-ink">Backdrop click + Esc close; body scroll-locks; rises from the bottom on mobile.</p>
          </Modal>
        </Card>
      </Section>

      <footer className="mt-12 border-t border-edge pt-4 font-mono text-[11px] text-muted">
        Batonkeep design system — D-0006. Compose new surfaces from <code className="text-amber">src/ui/</code>.
      </footer>
    </div>
  );
}
