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

const SHIELD = "M12 2.4 L19.4 5.2 L19.4 11.4 C19.4 16 16.1 19.5 12 21.6 C7.9 19.5 4.6 16 4.6 11.4 L4.6 5.2 Z";

/**
 * Candidate mark: baton in a shield. The shield = "keep" (a fortified stronghold
 * → custody / sovereignty); the baton = orchestration. Captures both halves of
 * the name. Shield in currentColor; baton in the accent token (themes with the
 * chosen accent). `relay` swaps the vertical baton for the diagonal hand-off.
 */
export function ShieldMark({
  size = 24,
  className = "",
  relay = false,
  title,
}: MarkProps & { relay?: boolean }) {
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
      <path d={SHIELD} stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" opacity="0.85" />
      {relay ? (
        <>
          <path d="M9.4 14.6 L14.6 9.4" className="stroke-amber" strokeWidth="2.4" strokeLinecap="round" />
          <circle cx="8.2" cy="15.8" r="1.5" className="fill-amber" opacity="0.4" />
          <circle cx="15.8" cy="8.2" r="1.5" className="fill-amber" />
        </>
      ) : (
        <path d="M12 7 L12 15.2" className="stroke-amber" strokeWidth="2.7" strokeLinecap="round" />
      )}
    </svg>
  );
}

/**
 * Solid-shield mark: the shield is filled with the accent (the brand colour); the
 * baton (node·shaft·node) is knocked out to the page background, so it stays
 * high-contrast on any accent and reads cleanly at favicon size. This is the
 * "shield colour = primary" lockup.
 */
export function ShieldSolid({ size = 24, className = "", title }: MarkProps) {
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
      {/* shield = brand colour */}
      <path d="M5 4 L19 4 L19 12.6 C19 18 12 21.6 12 21.6 C12 21.6 5 18 5 12.6 Z" className="fill-amber" />
      {/* relay baton, knocked out to the page background: solid disc (origin) →
          open channel (shaft) → ring (target). Solid→hollow reads as a hand-off. */}
      <g className="fill-base stroke-base" strokeWidth="0.95">
        <circle cx="12" cy="8" r="1.7" stroke="none" />
        <rect x="11.15" y="9.7" width="1.7" height="5.7" rx="0.85" fill="none" />
        <circle cx="12" cy="17.5" r="1.55" fill="none" />
      </g>
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
