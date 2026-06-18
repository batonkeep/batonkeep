// UsageCheckModal.tsx — D-0049: "Check usage" disclaimer + two-path chooser.
//
// The full-TTY background scrape is gone. Provider quota % is no longer displayed.
// This modal gives users a manual one-shot path: read the disclaimer, then choose:
//   (A) "Capture for me" — run the usage command as a one-shot and display raw
//       output in the modal (no pyte parsing, raw output shown as-is).
//   (B) "Open terminal" — open a sessionless provider terminal (/ws/console-tty)
//       pre-filled with the usage command. The user runs it themselves.
//       More reliable when auto-capture is broken (Grok/agy format changes).
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { AlertTriangle, Copy, Check as CheckIcon, Terminal, Zap, X } from "lucide-react";
import { api } from "../api";
import { Button } from "../ui";

const ConsoleTtyConsole = lazy(() => import("./ConsoleTtyConsole"));

type ModalState =
  | { phase: "disclaimer" }
  | { phase: "capturing" }
  | { phase: "captured"; output: string; truncated: boolean }
  | { phase: "capture-error"; message: string }
  | { phase: "terminal"; command: string };

interface Props {
  instanceId: string; // provider instance id, e.g. "claude" / "grok"
  instanceLabel: string;
  token: string; // console token (gates the PTY spawn)
  appAuthEnabled: boolean;
  onClose: () => void;
}

export default function UsageCheckModal({
  instanceId,
  instanceLabel,
  token,
  onClose,
}: Props) {
  const [state, setState] = useState<ModalState>({ phase: "disclaimer" });
  const [usageCommand, setUsageCommand] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const outputRef = useRef<HTMLDivElement>(null);

  // Fetch the usage command eagerly so we have it ready for both paths.
  useEffect(() => {
    api.getProviderUsageCommand(instanceId)
      .then((r) => setUsageCommand(r.command))
      .catch(() => setUsageCommand("/usage")); // safe fallback
  }, [instanceId]);

  // Auto-scroll as output arrives.
  useEffect(() => {
    if (state.phase === "captured" && outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [state]);

  const handleCapture = async () => {
    setState({ phase: "capturing" });
    try {
      const res = await api.captureUsageRaw(instanceId, token);
      setState({
        phase: "captured",
        output: res.output,
        truncated: res.truncated ?? false,
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setState({ phase: "capture-error", message: msg });
    }
  };

  const handleOpenTerminal = () => {
    setState({ phase: "terminal", command: usageCommand ?? "/usage" });
  };

  const handleCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* clipboard unavailable */ }
  };

  // ── Terminal phase: full-screen sessionless provider TTY ─────────────────────
  if (state.phase === "terminal") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
        <div className="flex h-[80vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-edge bg-base shadow-2xl">
          <div className="flex items-center justify-between border-b border-edge px-4 py-2.5">
            <div className="flex items-center gap-2">
              <Terminal size={14} className="text-brand" />
              <span className="font-mono text-sm text-ink">
                {instanceLabel} · usage terminal
              </span>
            </div>
            <button
              onClick={onClose}
              className="rounded p-1 text-muted hover:text-ink transition-colors"
            >
              <X size={14} />
            </button>
          </div>
          {/* Hint bar — command to type */}
          <div className="flex items-center justify-between border-b border-edge bg-surface px-4 py-2">
            <span className="font-mono text-[11px] text-muted">
              Type{" "}
              <span className="rounded bg-edge px-1 py-0.5 font-mono text-[11px] text-ink">
                {state.command}
              </span>{" "}
              and press Enter to view your usage.
            </span>
            <button
              onClick={() => handleCopy(state.command)}
              title="Copy command"
              className="flex items-center gap-1 font-mono text-[10px] text-muted hover:text-ink transition-colors"
            >
              {copied ? <CheckIcon size={11} className="text-ok" /> : <Copy size={11} />}
              {copied ? "copied" : "copy"}
            </button>
          </div>
          <div className="flex-1 overflow-hidden">
            <Suspense fallback={null}>
              <ConsoleTtyConsole
                instance={instanceId}
                token={token}
                onClose={onClose}
                embedded
                subtitle={`type ${state.command} · enter to run`}
              />
            </Suspense>
          </div>
        </div>
      </div>
    );
  }

  // ── Main modal ───────────────────────────────────────────────────────────────
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-md rounded-xl border border-edge bg-base shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-edge px-4 py-3">
          <div className="flex items-center gap-2">
            <AlertTriangle size={14} className="text-amber-400" />
            <span className="font-mono text-sm font-semibold text-ink">
              Check usage · {instanceLabel}
            </span>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-muted hover:text-ink transition-colors"
          >
            <X size={14} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {/* Disclaimer — amber, fully opaque and visible */}
          <div className="rounded-lg border border-amber-400/40 bg-amber-400/10 p-3 space-y-1.5">
            <p className="font-mono text-[11px] font-semibold uppercase tracking-wider text-amber-400">
              Accuracy disclaimer
            </p>
            <ul className="space-y-1 text-xs text-muted">
              <li>· Usage data is fetched by running the provider's own CLI command.</li>
              <li>· The output format is volatile and changes without notice.</li>
              <li>· Results may be inaccurate or missing if the format has changed.</li>
              <li>· For authoritative quota info, use the provider's own dashboard.</li>
            </ul>
          </div>

          {/* Capturing state */}
          {state.phase === "capturing" && (
            <div className="flex items-center gap-2 rounded-lg border border-edge bg-surface px-4 py-3">
              <div className="h-2 w-2 animate-pulse rounded-full bg-brand" />
              <span className="font-mono text-xs text-muted">
                Running {usageCommand ?? "/usage"} and capturing output…
              </span>
            </div>
          )}

          {/* Captured output */}
          {state.phase === "captured" && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
                  Raw output (uninterpreted)
                </span>
                {state.truncated && (
                  <span className="font-mono text-[10px] text-defer">truncated</span>
                )}
              </div>
              <div
                ref={outputRef}
                className="max-h-48 overflow-auto rounded border border-edge bg-zinc-950 p-3 font-mono text-[11px] leading-relaxed text-zinc-300 whitespace-pre-wrap"
              >
                {state.output || <span className="text-muted italic">no output captured</span>}
              </div>
              <p className="text-[10px] text-muted italic">
                Output shown as-is. If this looks wrong, use "Open terminal" to run it yourself.
              </p>
            </div>
          )}

          {/* Capture error */}
          {state.phase === "capture-error" && (
            <div className="rounded-lg border border-bad/30 bg-bad/5 p-3">
              <p className="font-mono text-[11px] text-bad">
                Capture failed: {state.message}
              </p>
              <p className="mt-1 text-xs text-muted">
                Try "Open terminal" to run the command yourself.
              </p>
            </div>
          )}

          {/* Action buttons — disclaimer + error phases */}
          {(state.phase === "disclaimer" || state.phase === "capture-error") && (
            <div className="space-y-2 pt-1">
              <Button
                variant="outline"
                size="sm"
                className="w-full justify-start gap-2"
                icon={<Zap size={13} className="text-brand" />}
                onClick={handleCapture}
              >
                <span className="flex-1 text-left">
                  <span className="text-ink">Capture for me</span>
                  <span className="ml-1.5 font-mono text-[10px] text-muted">
                    run {usageCommand ?? "/usage"} and show raw output here
                  </span>
                </span>
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="w-full justify-start gap-2"
                icon={<Terminal size={13} className="text-muted" />}
                onClick={handleOpenTerminal}
              >
                <span className="flex-1 text-left">
                  <span className="text-ink">Open terminal</span>
                  <span className="ml-1.5 font-mono text-[10px] text-muted">
                    run it yourself · more reliable when format has changed
                  </span>
                </span>
              </Button>
              <button
                onClick={onClose}
                className="w-full pt-1 text-center font-mono text-[11px] text-muted hover:text-ink transition-colors"
              >
                cancel
              </button>
            </div>
          )}

          {/* After capture: offer to open terminal instead */}
          {state.phase === "captured" && (
            <div className="flex items-center justify-between gap-3 pt-1">
              <button
                onClick={handleOpenTerminal}
                className="flex items-center gap-1 font-mono text-[11px] text-muted hover:text-brand transition-colors"
              >
                <Terminal size={11} /> open terminal instead
              </button>
              <button
                onClick={onClose}
                className="font-mono text-[11px] text-muted hover:text-ink transition-colors"
              >
                close
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
