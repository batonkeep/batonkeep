// Logo.tsx — the Batonkeep brand mark + wordmark.
//
// Concept: a relay baton mid-hand-off. The diagonal capsule is the baton; the
// two station nodes are the passing agent (faded, bottom-left) and the receiving
// agent (solid, top-right). It encodes the product's core motion — work handed
// off between agents, with failover across owned plans. Monochrome `currentColor`
// so it themes: set the color on the parent (e.g. `text-amber` for brand).

interface MarkProps {
  size?: number;
  className?: string;
  title?: string;
}

/** The bare relay-baton mark. Inherits color from `currentColor`. */
export function LogoMark({ size = 24, className = "", title }: MarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      {title ? <title>{title}</title> : null}
      {/* the baton — a diagonal capsule */}
      <path
        d="M7.5 16.5 L16.5 7.5"
        stroke="currentColor"
        strokeWidth="3.6"
        strokeLinecap="round"
      />
      {/* passing station (motion origin) */}
      <circle cx="5" cy="19" r="2.1" fill="currentColor" opacity="0.4" />
      {/* receiving station (hand-off target) */}
      <circle cx="19" cy="5" r="2.1" fill="currentColor" />
    </svg>
  );
}

interface LogoProps {
  /** Render the wordmark beside the mark. */
  wordmark?: boolean;
  size?: number;
  className?: string;
}

/**
 * Full lockup: mark + "batonkeep" wordmark. The mark carries the amber signal;
 * the wordmark sits in ink with `keep` dimmed to muted, reinforcing baton·keep.
 */
export default function Logo({ wordmark = true, size = 22, className = "" }: LogoProps) {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <LogoMark size={size} className="text-amber" title="Batonkeep" />
      {wordmark ? (
        <span className="font-mono font-semibold tracking-tight text-ink">
          baton<span className="text-muted">keep</span>
        </span>
      ) : null}
    </span>
  );
}
