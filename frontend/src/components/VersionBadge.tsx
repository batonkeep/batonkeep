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

  const running = info.version.startsWith("v") || info.version === "dev"
    ? info.version
    : `v${info.version}`;
  const releaseUrl = info.release_url || RELEASES_URL;

  if (compact) {
    // Desktop nav footer: single muted line, version links to releases.
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
        {info.update_available && info.latest && (
          <a
            href={info.release_url || RELEASES_URL}
            target="_blank"
            rel="noreferrer"
            className="text-brand hover:underline"
            title={`Update available: ${info.latest}`}
          >
            · {info.latest} available
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
        {info.update_available && info.latest ? (
          <span className="text-[11px] text-brand">{info.latest} available</span>
        ) : (
          info.version !== "dev" && (
            <span className="text-[11px] text-muted">up to date</span>
          )
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
