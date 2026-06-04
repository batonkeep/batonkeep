// StatusDot.tsx — a small state indicator. `pulse` is reserved for live/streaming
// (cyan), matching the rule that cyan signals activity and nothing else.
import type { Tone } from "./Badge";

const DOT: Record<Tone, string> = {
  neutral: "bg-muted",
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  live: "bg-live",
  defer: "bg-defer",
  brand: "bg-brand",
};

interface Props {
  tone?: Tone;
  pulse?: boolean;
  size?: number;
  className?: string;
}

export default function StatusDot({ tone = "neutral", pulse = false, size = 8, className = "" }: Props) {
  return (
    <span
      className={`inline-block rounded-full ${DOT[tone]} ${pulse ? "animate-pulse-live" : ""} ${className}`}
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  );
}
