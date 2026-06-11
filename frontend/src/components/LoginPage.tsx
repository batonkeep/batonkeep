// LoginPage.tsx — app-level auth splash (D-0023, resolves P-0026).
// D-0027 follow-up: elevated to a full brand splash — large mark, tagline,
// clean centred card. Shown when APP_PASSWORD is set and no session exists.
// NOTE: distinct from the *generated* demo landing page (D-0007).
import { useState } from "react";
import { Lock } from "lucide-react";
import { api } from "../api";
import { BatonMark } from "../ui/Logo";
import { Button, Input } from "../ui";

export default function LoginPage({ onAuthed }: { onAuthed: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      onAuthed();
    } catch {
      setError("Incorrect password.");
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden bg-base px-4">
      {/* Subtle radial glow behind the logo */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse 60% 45% at 50% 38%, rgba(255,170,0,0.07) 0%, transparent 70%)",
        }}
      />

      {/* Brand mark — large, centred */}
      <div className="relative mb-6 flex flex-col items-center gap-4">
        <BatonMark size={88} title="batonkeep" />
        <div className="text-center">
          <h1 className="font-mono text-2xl font-semibold tracking-tight text-ink">
            baton<span className="text-muted">keep</span>
          </h1>
          <p className="mt-1.5 max-w-xs text-sm text-muted">
            Your plans, your keys, your machine.
            <br />
            Switch agents mid-task. Keep the work.
          </p>
        </div>
      </div>

      {/* Sign-in card */}
      <form
        onSubmit={submit}
        className="relative w-full max-w-xs space-y-4 rounded-2xl border border-edge bg-panel/70 p-7 shadow-xl backdrop-blur-sm"
      >
        <div className="space-y-2">
          <Input
            type="password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Workspace password"
            aria-label="Password"
          />
          {error && <p className="text-xs text-bad">{error}</p>}
        </div>
        <Button
          type="submit"
          variant="primary"
          icon={<Lock size={14} />}
          className="w-full"
          disabled={busy || password.length === 0}
        >
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>

      {/* Footer note */}
      <p className="relative mt-6 text-center text-[11px] text-muted/60">
        Local deployment · no data leaves this machine
      </p>
    </div>
  );
}
