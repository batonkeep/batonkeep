// LoginPage.tsx — app-level auth landing gate (D-0023, resolves P-0026).
// Shown when the backend has APP_PASSWORD set and no valid session cookie is
// present. A single-operator password unlocks the whole app (it protects the
// data, not just the scoped console). NOTE: distinct from the *generated*
// landing page of the flagship demo (D-0007) — this is Batonkeep's own gate.
import { useState } from "react";
import { Lock } from "lucide-react";
import { api } from "../api";
import { Button, Input, Logo } from "../ui";

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
    <div className="flex min-h-screen items-center justify-center bg-base px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-6 rounded-xl border border-edge bg-surface/60 p-8 shadow-sm"
      >
        <div className="flex flex-col items-center gap-3 text-center">
          <Logo />
          <p className="text-sm text-muted">Sign in to your workspace</p>
        </div>
        <div className="space-y-2">
          <Input
            type="password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            aria-label="Password"
          />
          {error && <p className="text-xs text-bad">{error}</p>}
        </div>
        <Button
          type="submit"
          variant="primary"
          icon={<Lock size={15} />}
          className="w-full"
          disabled={busy || password.length === 0}
        >
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
