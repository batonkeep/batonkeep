// Badge.tsx — compact status/label pill. Tones map to the semantic tokens so
// status colour is consistent everywhere (run states, provider health, tiers).
import type { ReactNode } from "react";

export type Tone = "neutral" | "ok" | "warn" | "bad" | "live" | "defer" | "brand";

const TONES: Record<Tone, string> = {
  neutral: "bg-ink/5 text-muted border-edge",
  ok: "bg-ok/10 text-ok border-ok/30",
  warn: "bg-warn/10 text-warn border-warn/30",
  bad: "bg-bad/10 text-bad border-bad/30",
  live: "bg-live/10 text-live border-live/30",
  defer: "bg-defer/10 text-defer border-defer/30",
  brand: "bg-brand/10 text-brand border-brand/30",
};

interface Props {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}

export default function Badge({ tone = "neutral", children, className = "" }: Props) {
  return (
    <span
      className={
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 " +
        `font-mono text-[11px] font-medium leading-none ${TONES[tone]} ${className}`
      }
    >
      {children}
    </span>
  );
}
