// Logo.tsx — the batonkeep brand mark + wordmark.
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

/** Logo red — hardcoded, independent of the UI accent (teal). */
const LOGO_RED = "#ff4f4f";

/**
 * The real batonkeep mark — the Inkscape-authored SVG. Fill is the logo red
 * (#ff4f4f), NOT the UI accent token, so the mark stays red regardless of
 * which teal variant is active on interactive elements.
 */
export function BatonMark({ size = 48, className = "", title }: MarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="249 100 386 386"
      fill="none"
      className={className}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      {title ? <title>{title}</title> : null}
      <path
        fill={LOGO_RED}
        fillRule="evenodd"
        d="M 427,470.16653 C 357.39351,429.30567 317.62351,378.90653 300.61244,310 292.67417,277.84455 290.16139,254.01862 289.30934,202.82511 l -0.62706,-37.67488 3.40886,-0.51365 c 1.87487,-0.28251 9.15734,-1.22456 16.18327,-2.09345 45.49594,-5.62645 89.27741,-20.74318 123.97018,-42.80404 l 7.74459,-4.92473 9.0191,5.69086 c 25.0409,15.80028 58.32318,29.16587 90.94187,36.52067 12.75375,2.8757 37.14511,6.69621 46.04985,7.21297 l 4.5,0.26114 -0.14668,37.12897 c -0.19344,48.96582 -2.4924,73.24911 -9.8337,103.87103 C 564.84475,370.88272 530.03844,420.00841 472,458.66478 c -9.36137,6.23511 -30.77419,18.34933 -32.33587,18.29389 -0.64027,-0.0227 -6.33913,-3.07919 -12.66413,-6.79214 z m 22.00168,-56.16739 c 12.75392,-6.50656 15.75975,-21.61617 6.3682,-32.01149 -9.3838,-10.38674 -26.10063,-8.59455 -33.14977,3.55394 -1.92598,3.31923 -2.24775,5.11724 -1.99276,11.13502 0.26619,6.28191 0.688,7.65327 3.38344,11 6.52035,8.09585 16.86549,10.67187 25.39089,6.32253 z m -24.53602,-43.11138 c 1.96898,-1.16173 5.48221,-2.67386 7.80716,-3.36028 l 4.22718,-1.24805 0.25747,-62.52504 0.25747,-62.52504 2.24253,-0.60102 c 1.23339,-0.33056 2.80503,-0.60717 3.49253,-0.61468 0.98702,-0.0108 1.25,13.34573 1.25,63.48635 V 367 h 2.35908 c 1.29749,0 4.89749,1.2804 8,2.84534 L 460,372.69068 V 298.84534 225 h -8 -8 v -6.36414 -6.36414 l 4.48165,-1.71156 c 2.67828,-1.02285 6.04515,-3.4878 8.36722,-6.12581 11.62572,-13.20751 5.48494,-32.92286 -11.65309,-37.41295 -17.10112,-4.48042 -32.46091,12.62141 -26.75763,29.79238 1.94795,5.86474 8.24206,12.26552 13.89599,14.13148 L 436,212.1551 V 218.57755 225 h -8 -8 v 74 c 0,40.7 0.19928,74 0.44284,74 0.24356,0 2.05383,-0.95051 4.02282,-2.11224 z"
      />
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
 * Full lockup: BatonMark (logo red) + "batonkeep" wordmark.
 */
export default function Logo({ wordmark = true, size = 22, className = "" }: LogoProps) {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <BatonMark size={size} title="batonkeep" />
      {wordmark ? (
        <span className="font-mono font-semibold tracking-tight text-ink">
          baton<span className="text-muted">keep</span>
        </span>
      ) : null}
    </span>
  );
}
