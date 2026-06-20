// VersionBadge.tsx — shows the running Batonkeep version with a GitHub link, and
// an inline "update available" hint when the instance is behind the latest
// release (D-0053). No nag/banner — just an unobtrusive indicator. Two layouts:
// `compact` for the desktop nav footer, default for the Settings card.
import { ArrowUpRight } from "lucide-react";
import { useVersion } from "../useVersion";

const RELEASES_URL = "https://github.com/batonkeep/batonkeep/releases";

export default function VersionBadge({ compact = false }: { compact?: boolean }) {
  const info = useVersion();
  if (!info) return null;

  const isDev = info.version === "dev";
  const running = isDev || info.version.startsWith("v") ? info.version : `v${info.version}`;
  const releaseUrl = info.release_url || RELEASES_URL;

  // Status hint, computed once for both layouts:
  // - real release behind latest      → "vY available" (brand, actionable)
  // - dev/unstamped build, latest known → "latest vY" (neutral reference: a dev
  //   build may be ahead OR behind, so it's information, not a nag)
  // - real release, current            → "up to date"
  // - latest unknown (check off/down)  → nothing
  let hint: { text: string; tone: "brand" | "muted" } | null = null;
  if (info.update_available && info.latest) {
    hint = { text: `${info.latest} available`, tone: "brand" };
  } else if (isDev && info.latest) {
    hint = { text: `latest ${info.latest}`, tone: "muted" };
  } else if (!isDev && info.latest) {
    hint = { text: "up to date", tone: "muted" };
  }

  if (compact) {
    // Desktop nav footer: single line, version links to releases.
    return (
      <div className="hidden md:flex md:items-center md:gap-1.5 md:px-3 md:pb-1 md:text-[10px] md:text-muted">
        <a
          href={releaseUrl}
          target="_blank"
          rel="noreferrer"
          className="font-mono hover:text-ink transition-colors"
          title="View release notes on GitHub"
        >
          {running}
        </a>
        {hint && (
          <a
            href={releaseUrl}
            target="_blank"
            rel="noreferrer"
            className={`${hint.tone === "brand" ? "text-brand hover:underline" : "hover:text-ink transition-colors"}`}
            title={hint.tone === "brand" ? `Update available: ${info.latest}` : `Latest release: ${info.latest}`}
          >
            · {hint.text}
          </a>
        )}
      </div>
    );
  }

  // Settings card: labelled row with a GitHub link for details.
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <div className="flex items-center gap-2">
        <span className="font-mono text-ink">{running}</span>
        {isDev && <span className="text-[11px] text-muted">(local build)</span>}
        {hint && (
          <span className={`text-[11px] ${hint.tone === "brand" ? "text-brand" : "text-muted"}`}>
            {hint.text}
          </span>
        )}
      </div>
      <a
        href={releaseUrl}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-ink transition-colors"
      >
        Release notes <ArrowUpRight size={12} />
      </a>
    </div>
  );
}
