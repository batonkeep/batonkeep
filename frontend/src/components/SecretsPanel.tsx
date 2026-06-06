// SecretsPanel.tsx — the named secrets-management surface (P-0009 #3).
// One place that reports, for every key-backed provider, where its credential
// resolves from (encrypted store / deployment env / missing), a masked last-4
// hint, and when it was last used. Never shows any plaintext.
// D-track: composed from ui/ primitives (Badge, Card, StatusDot).
import { useCallback, useEffect, useState } from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import type { SecretStatus } from "../types";
import { api } from "../api";
import { Badge, Button, Card, StatusDot } from "../ui";

const SOURCE_TONE: Record<SecretStatus["source"], "ok" | "neutral" | "bad"> = {
  stored: "ok",
  env: "neutral",
  missing: "bad",
};

const SOURCE_LABEL: Record<SecretStatus["source"], string> = {
  stored: "stored key",
  env: "deployment env",
  missing: "no key",
};

function lastUsed(ts: string | null): string {
  if (!ts) return "never used";
  const d = new Date(ts);
  return `last used ${d.toLocaleDateString()}`;
}

export default function SecretsPanel() {
  const [rows, setRows] = useState<SecretStatus[]>([]);
  const load = useCallback(() => api.getSecretsStatus().then(setRows).catch(() => { }), []);
  useEffect(() => { load(); }, [load]);

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldCheck size={15} className="text-brand" />
          <span className="font-mono text-sm font-semibold text-ink">provider keys</span>
        </div>
        <Button variant="outline" size="sm" icon={<RefreshCw size={12} />} onClick={load}>
          Refresh
        </Button>
      </div>
      <p className="mt-1 text-xs text-muted">
        Where each provider's API key resolves from — never the key itself. BYO keys are
        encrypted at rest; <span className="text-brand">local</span> providers need no remote key.
      </p>

      <div className="mt-3 divide-y divide-edge">
        {rows.length === 0 && (
          <p className="py-3 font-mono text-xs text-muted">no key-backed providers</p>
        )}
        {rows.map((r) => (
          <div key={r.provider} className="flex items-center justify-between gap-2 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <StatusDot tone={r.source === "missing" ? "bad" : "ok"} />
              <span className="truncate font-mono text-sm text-ink">{r.provider}</span>
              {r.local && <Badge tone="ok">local</Badge>}
              {r.key_hint && <span className="font-mono text-[11px] text-muted">{r.key_hint}</span>}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="hidden font-mono text-[10px] text-muted sm:inline">{lastUsed(r.last_used_at)}</span>
              <Badge tone={SOURCE_TONE[r.source]}>{SOURCE_LABEL[r.source]}</Badge>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
