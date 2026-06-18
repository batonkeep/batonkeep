// ConsoleTtyConsole.tsx — sessionless provider terminal (D-0049 / Settings panel).
//
// Like WebTtyConsole but uses /ws/console-tty instead of /ws/tty — no session
// workspace is needed. Used for:
//   - The Settings panel "Open terminal" button on provider cards (run /usage,
//     inspect auth state, etc.)
//   - The UsageCheckModal "Open terminal" path
//
// The terminal plumbing lives in PtyTerminal (shared with auth + tty consoles).
import PtyTerminal from "./PtyTerminal";

interface Props {
  instance: string; // provider instance id, e.g. "agy" / "claude:work"
  token: string; // console token (gates the privileged PTY spawn)
  onClose: () => void;
  embedded?: boolean; // inline vs overlay modal
  subtitle?: string;
}

export default function ConsoleTtyConsole({
  instance,
  token,
  onClose,
  embedded,
  subtitle = "you drive every turn · no session context",
}: Props) {
  return (
    <PtyTerminal
      wsPath="/ws/console-tty"
      init={{ token, instance }}
      title={<>terminal · <span className="text-brand">{instance}</span></>}
      subtitle={subtitle}
      onClose={onClose}
      embedded={embedded}
    />
  );
}
