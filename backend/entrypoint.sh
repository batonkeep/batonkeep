#!/usr/bin/env bash
# entrypoint.sh — one-shot volume migration, then drop to batond (P-0022/D-0020).
#
# The privilege split (single `agent` → `batond` + `sandbox`) re-owns data that
# already exists on the persistent appdata/agent_home volumes: the former `agent`
# uid (1000) is now `sandbox`, so control-plane state (/data root, the DB) must be
# re-owned to `batond` or the backend can't read it, and shared trees must move to
# the `agents` group. This runs as root at container start, fixes ownership
# idempotently, then execs the long-running backend as the non-root `batond` user
# (only this brief init is privileged; the network-facing process never is).
#
# Idempotent: safe to run on every boot. On a fresh volume it's a near no-op.
set -euo pipefail

run_migration() {
  echo "[entrypoint] P-0022 fs-isolation migration…"

  # Control plane → batond, unreadable to sandbox.
  chown -R batond:batond /app 2>/dev/null || true
  # /data root + DB + provider config: batond-owned. Tighten the DB and provider
  # instances to 0640 (batond rw, group r) — sandbox is NOT in batond's group.
  chown batond:batond /data 2>/dev/null || true
  for f in /data/batonkeep.db /data/provider-instances.json /data/exec-seam-overrides.json; do
    [ -e "$f" ] && chown batond:batond "$f" && chmod 0640 "$f" || true
  done
  chown -R batond:batond /data/outputs 2>/dev/null || true
  # Published share bundles (M1.4) are materialized + served by the backend only
  # (never the sandbox), so this dir is batond-owned like /data/outputs. Create it
  # here so a pre-existing root-owned dir (older image/volume) can't leave the
  # backend unable to makedirs a token subdir (EACCES on POST …/publish).
  mkdir -p /data/publish
  chown -R batond:batond /data/publish 2>/dev/null || true

  # Shared lanes → agents group, setgid, group-writable (batond + sandbox co-write).
  for d in /data/sessions /work; do
    mkdir -p "$d"
    chown -R batond:agents "$d" 2>/dev/null || true
    find "$d" -type d -exec chmod 2770 {} + 2>/dev/null || true
    find "$d" -type f -exec chmod 0660 {} + 2>/dev/null || true
  done

  # CLI auth / OAuth home stays with sandbox (uid 1000 unchanged across the split).
  chown -R sandbox:sandbox /home/agent 2>/dev/null || true

  # Stale-data hygiene (decision d): drop the legacy aicadence DB and any old
  # run_* outputs that predate the isolated-workspace layout so the agent can
  # never stumble onto and narrate them.
  rm -f /data/aicadence.db /data/aicadence.db-* 2>/dev/null || true

  echo "[entrypoint] migration done."
}

if [ "$(id -u)" = "0" ]; then
  run_migration
  # Give the backend process its HOME explicitly. We deliberately do NOT set a
  # container-wide HOME (Dockerfile/compose), so `docker exec -u sandbox` and agent
  # subprocesses resolve HOME from /etc/passwd (sandbox→/home/agent). Only the
  # backend needs batond's home, so set it here at the one exec boundary.
  exec gosu batond:batond env HOME=/home/batond "$@"
else
  # Already unprivileged (e.g. local dev without root) — skip migration, run as-is.
  echo "[entrypoint] not root; skipping migration, running as $(id -un)."
  exec "$@"
fi
