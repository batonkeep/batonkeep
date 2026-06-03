// AuthConsole.tsx — scoped auth runner. Streams the backend's fixed auth.sh PTY
// over /ws/console (token-gated) into a real terminal (xterm.js), so interactive
// logins (device codes, redirect URLs, TUI pickers) render correctly and accept
// keystrokes. Deliberately not a general shell — the backend only ever runs
// auth.sh with one validated target, never an arbitrary command.
import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

interface Props {
  target: string; // instance id or template, e.g. "claude" / "claude:work"
  token: string;
  onClose: () => void;
}

// Mission-control palette (matches the app's near-black + amber theme).
const THEME = {
  background: "#0a0b0d",
  foreground: "#e8e6e1",
  cursor: "#f5b700",
  brightBlack: "#5a5e66",
};

export default function AuthConsole({ target, token, onClose }: Props) {
  const [status, setStatus] = useState<"connecting" | "running" | "done" | "error">("connecting");
  const mountRef = useRef<HTMLDivElement | null>(null);

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
    const ws = new WebSocket(`${proto}://${location.host}/ws/console`);

    const sendResize = () => {
      try {
        fit.fit();
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
        }
      } catch { /* ignore */ }
    };

    ws.onopen = () => {
      ws.send(JSON.stringify({ token, target, rows: term.rows, cols: term.cols }));
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
  }, [target, token]);

  const statusColor =
    status === "running" ? "text-live" : status === "error" ? "text-bad" : status === "done" ? "text-ok" : "text-muted";

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm md:items-center md:p-4">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col rounded-t-2xl border border-edge bg-base md:rounded-2xl">
        <div className="flex items-center justify-between border-b border-edge px-5 py-3">
          <div>
            <h2 className="font-mono text-sm font-semibold text-ink">
              auth · <span className="text-amber">{target}</span>
            </h2>
            <p className={`text-[11px] ${statusColor}`}>{status} · type directly into the terminal to answer prompts</p>
          </div>
          <button onClick={onClose} className="text-muted hover:text-ink"><X size={18} /></button>
        </div>

        <div ref={mountRef} className="h-[60vh] flex-1 overflow-hidden bg-[#0a0b0d] p-2" />
      </div>
    </div>
  );
}
