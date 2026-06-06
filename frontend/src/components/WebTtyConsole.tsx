// WebTtyConsole.tsx — human-driven web-TTY (D-0016 seam #3 / D-0017). Streams a
// real provider CLI launched in this session's workspace over /ws/tty into a
// terminal the user types into directly. No prompt injection — the human drives
// every turn (the ToS-clean interactive lane). Terminal plumbing: PtyTerminal.
import PtyTerminal from "./PtyTerminal";

interface Props {
  session: string; // session id (workspace cwd on the backend)
  instance: string; // provider instance id, e.g. "claude" / "claude:work"
  token: string; // console token (gates the privileged PTY spawn)
  onClose: () => void;
  embedded?: boolean; // inline (in-session Terminal mode) vs overlay modal
}

export default function WebTtyConsole({ session, instance, token, onClose, embedded }: Props) {
  return (
    <PtyTerminal
      wsPath="/ws/tty"
      init={{ token, session, instance }}
      title={<>terminal · <span className="text-brand">{instance}</span></>}
      subtitle="you drive every turn"
      onClose={onClose}
      embedded={embedded}
    />
  );
}
