// TotpPanel.tsx — Settings → Security: optional TOTP second factor (D-0056,
// resolves P-0062). Enroll via QR (rendered client-side from the otpauth URI —
// the secret never round-trips through a third party) or manual key entry;
// one live code confirms enrollment before login starts requiring it.
// Recovery is env break-glass only: TOTP_DISABLED=1 (self-hosted operators
// have host access; no recovery codes in V1).
import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Copy, ShieldCheck, ShieldOff } from "lucide-react";
import qrcode from "qrcode-generator";
import { api } from "../api";
import type { TotpSetup, TotpStatus } from "../types";
import { Badge, Button, Card, Input } from "../ui";

function QrImage({ uri }: { uri: string }) {
  // White quiet-zone padding keeps the code scannable on the dark theme.
  const src = useMemo(() => {
    const qr = qrcode(0, "M");
    qr.addData(uri);
    qr.make();
    return qr.createDataURL(4, 8);
  }, [uri]);
  return (
    <img
      src={src}
      alt="TOTP enrollment QR code"
      className="h-44 w-44 rounded-lg border border-edge bg-white p-1"
    />
  );
}

function CodeInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Input
      type="text"
      inputMode="numeric"
      autoComplete="one-time-code"
      maxLength={6}
      value={value}
      onChange={(e) => onChange(e.target.value.replace(/\D/g, ""))}
      placeholder="6-digit code"
      aria-label="TOTP code"
      className="w-32 font-mono"
    />
  );
}

export default function TotpPanel({ appAuthEnabled }: { appAuthEnabled: boolean }) {
  const [status, setStatus] = useState<TotpStatus | null>(null);
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(() => {
    api.getTotpStatus().then(setStatus).catch(() => setStatus(null));
  }, []);
  useEffect(() => {
    if (appAuthEnabled) refresh();
  }, [appAuthEnabled, refresh]);

  if (!appAuthEnabled) {
    return (
      <Card className="px-4 py-3">
        <p className="text-sm text-muted">
          Two-factor authentication needs the app login gate first — set{" "}
          <code className="font-mono text-ink">APP_PASSWORD</code> in your deployment
          environment, then enroll here.
        </p>
      </Card>
    );
  }
  if (status === null) return <Card className="px-4 py-3 text-sm text-muted">Loading…</Card>;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const startSetup = () =>
    run(async () => {
      setSetup(await api.totpSetup());
      setCode("");
      refresh();
    });

  const activate = () =>
    run(async () => {
      setStatus(await api.totpActivate(code));
      setSetup(null);
      setCode("");
    });

  const disable = () =>
    run(async () => {
      setStatus(await api.totpDisable(code));
      setSetup(null);
      setCode("");
    });

  const copyKey = async () => {
    if (!setup) return;
    await navigator.clipboard.writeText(setup.secret);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Card className="space-y-3 px-4 py-3">
      {status.break_glass && (
        <p className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-ink">
          <code className="font-mono">TOTP_DISABLED</code> is set — the second factor is
          currently <strong>skipped at login</strong> (break-glass). Unset it to re-enforce.
        </p>
      )}

      {status.enabled ? (
        <>
          <div className="flex items-center gap-2">
            <ShieldCheck size={15} className="text-ok" />
            <span className="text-sm text-ink">Two-factor authentication is on</span>
            <Badge tone="ok">active</Badge>
          </div>
          <p className="text-[11px] text-muted">
            Login requires your password plus a 6-digit authenticator code. Lost the
            device? Set <code className="font-mono">TOTP_DISABLED=1</code> in the deployment
            environment to sign in with the password alone, then re-enroll.
          </p>
          <div className="flex items-center gap-2">
            <CodeInput value={code} onChange={setCode} />
            <Button
              variant="outline"
              size="sm"
              icon={<ShieldOff size={13} />}
              onClick={disable}
              disabled={busy || (!status.break_glass && code.length !== 6)}
            >
              Turn off
            </Button>
          </div>
          <p className="text-[11px] text-muted">Turning it off requires a current code.</p>
        </>
      ) : setup ? (
        <>
          <p className="text-sm text-ink">
            Scan the QR code with an authenticator app (or enter the key manually), then
            confirm with one code. Login keeps working with just the password until you
            confirm.
          </p>
          <div className="flex flex-col items-start gap-3 sm:flex-row">
            <QrImage uri={setup.otpauth_uri} />
            <div className="min-w-0 space-y-2">
              <p className="text-[11px] uppercase tracking-wider text-muted">Manual key</p>
              <div className="flex items-center gap-1.5">
                <code className="break-all rounded bg-base px-2 py-1 font-mono text-xs text-ink">
                  {setup.secret}
                </code>
                <Button
                  variant="ghost"
                  size="sm"
                  className="shrink-0 px-1.5"
                  icon={copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
                  onClick={copyKey}
                  title="Copy key"
                />
              </div>
              <div className="flex items-center gap-2 pt-1">
                <CodeInput value={code} onChange={setCode} />
                <Button
                  variant="primary"
                  size="sm"
                  onClick={activate}
                  disabled={busy || code.length !== 6}
                >
                  {busy ? "Confirming…" : "Confirm & enable"}
                </Button>
              </div>
            </div>
          </div>
        </>
      ) : (
        <>
          <p className="text-sm text-muted">
            Add a TOTP second factor (Google Authenticator, Aegis, 1Password, …) on top of
            the workspace password.
          </p>
          <Button variant="outline" size="sm" icon={<ShieldCheck size={13} />} onClick={startSetup} disabled={busy}>
            {status.pending ? "Restart setup" : "Enable two-factor"}
          </Button>
          {status.pending && (
            <p className="text-[11px] text-muted">
              A previous setup was never confirmed — restarting generates a fresh key.
            </p>
          )}
        </>
      )}

      {error && <p className="text-xs text-bad">{error}</p>}
    </Card>
  );
}
