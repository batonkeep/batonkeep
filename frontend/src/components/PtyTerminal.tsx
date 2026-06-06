// PtyTerminal.tsx — reusable xterm.js ↔ backend-PTY bridge over a WebSocket.
// Streams a backend PTY into a real terminal so interactive TUIs (device-code
// logins, provider CLIs) render correctly and accept keystrokes. The first frame
// sent is the caller's `init` payload (e.g. {token,target} for auth, or
// {token,session,instance} for web-TTY); thereafter the terminal IS the input.
// Consumers: AuthConsole (auth.sh) and WebTtyConsole (provider CLI). Deliberately
// not a general shell — the backend decides what each ws path may spawn.
import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

export type PtyStatus = "connecting" | "running" | "done" | "error";

interface Props {
  wsPath: string; // backend websocket path, e.g. "/ws/console" or "/ws/tty"
  init: Record<string, unknown>; // first frame (must carry the console token)
  title: React.ReactNode;
  subtitle?: string;
  onClose: () => void;
  // embedded: render inline (fills its parent), no modal chrome — for the
  // in-session Terminal mode. Default false = a centered overlay modal (auth).
  embedded?: boolean;
}

// Mission-control palette (matches the app's near-black + brand theme).
const THEME = {
  background: "#0a0b0d",
  foreground: "#e8e6e1",
  cursor: "#f5b700",
  brightBlack: "#5a5e66",
};

export default function PtyTerminal({ wsPath, init, title, subtitle, onClose, embedded = false }: Props) {
  const [status, setStatus] = useState<PtyStatus>("connecting");
  const mountRef = useRef<HTMLDivElement | null>(null);
  // Keep the latest init without re-opening the socket on every render.
  const initRef = useRef(init);
  initRef.current = init;

  useEffect(() => {
    const term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
      fontSize: 12,
      theme: THEME,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    if (mountRef.current) term.open(mountRef.current);
    try { fit.fit(); } catch { /* not yet laid out */ }

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}${wsPath}`);

    const sendResize = () => {
      try {
        fit.fit();
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
        }
      } catch { /* ignore */ }
    };

    ws.onopen = () => {
      ws.send(JSON.stringify({ ...initRef.current, rows: term.rows, cols: term.cols }));
      setStatus("running");
      term.focus();
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "output") term.write(msg.data);
        else if (msg.type === "exit") { term.write(`\r\n\x1b[90m[process exited with code ${msg.code ?? "?"}]\x1b[0m\r\n`); setStatus("done"); }
        else if (msg.type === "error") { term.write(`\r\n\x1b[31m[error: ${msg.message}]\x1b[0m\r\n`); setStatus("error"); }
      } catch { /* ignore malformed frame */ }
    };
    ws.onerror = () => setStatus("error");
    ws.onclose = () => setStatus((s) => (s === "running" ? "done" : s));

    // Forward every keystroke to the PTY (the terminal IS the input).
    const dataSub = term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data: d }));
    });

    window.addEventListener("resize", sendResize);

    return () => {
      window.removeEventListener("resize", sendResize);
      dataSub.dispose();
      ws.close();
      term.dispose();
    };
  }, [wsPath]);

  const statusColor =
    status === "running" ? "text-live" : status === "error" ? "text-bad" : status === "done" ? "text-ok" : "text-muted";

  // Embedded: fill the parent (in-session Terminal mode) with a slim status strip
  // and no close button — the host's Chat/Terminal toggle owns leaving the mode.
  if (embedded) {
    return (
      <div className="flex h-full flex-col overflow-hidden rounded-lg border border-edge bg-[#0a0b0d]">
        <div className="flex items-center justify-between border-b border-edge px-3 py-1.5">
          <span className="font-mono text-[11px] text-ink">{title}</span>
          <span className={`font-mono text-[10px] ${statusColor}`}>{status}{subtitle ? ` · ${subtitle}` : ""}</span>
        </div>
        <div ref={mountRef} className="flex-1 overflow-hidden p-2" />
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm md:items-center md:p-4">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col rounded-t-2xl border border-edge bg-base md:rounded-2xl">
        <div className="flex items-center justify-between border-b border-edge px-5 py-3">
          <div>
            <h2 className="font-mono text-sm font-semibold text-ink">{title}</h2>
            <p className={`text-[11px] ${statusColor}`}>{status}{subtitle ? ` · ${subtitle}` : ""}</p>
          </div>
          <button onClick={onClose} className="text-muted hover:text-ink"><X size={18} /></button>
        </div>

        <div ref={mountRef} className="h-[60vh] flex-1 overflow-hidden bg-[#0a0b0d] p-2" />
      </div>
    </div>
  );
}
