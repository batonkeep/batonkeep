// AuthConsole.tsx — scoped auth runner. Streams the backend's fixed auth.sh PTY
// over /ws/console (token-gated) into a real terminal, so interactive logins
// (device codes, redirect URLs, TUI pickers) render correctly and accept
// keystrokes. Deliberately not a general shell — the backend only ever runs
// auth.sh with one validated target. The terminal plumbing lives in PtyTerminal.
import PtyTerminal from "./PtyTerminal";

interface Props {
  target: string; // instance id or template, e.g. "claude" / "claude:work"
  token: string;
  onClose: () => void;
}

export default function AuthConsole({ target, token, onClose }: Props) {
  return (
    <PtyTerminal
      wsPath="/ws/console"
      init={{ token, target }}
      title={<>auth · <span className="text-brand">{target}</span></>}
      subtitle="type directly into the terminal to answer prompts"
      onClose={onClose}
    />
  );
}
