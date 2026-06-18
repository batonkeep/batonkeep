#!/usr/bin/env bash
# batonkeep-backup — D-0050: backup a running Batonkeep installation.
#
# Usage (from the directory containing docker-compose.yml):
#
#   # Write archive inside the container (then copy with docker compose cp):
#   docker compose exec backend bash /app/scripts/batonkeep-backup.sh
#
#   # Stream directly to a file on the host — no copy step needed:
#   docker compose exec -T backend bash /app/scripts/batonkeep-backup.sh --stdout > backup.tar.gz
#
#   # With a label:
#   docker compose exec -T backend bash /app/scripts/batonkeep-backup.sh --stdout --name before-upgrade > backup.tar.gz
#
#   # Dry-run (show what would be included):
#   docker compose exec backend bash /app/scripts/batonkeep-backup.sh --dry-run
#
# --stdout mode: tar stream is written to fd 1; all progress/log messages go to
# fd 2 (stderr) so they don't corrupt the binary stream. The manifest is embedded
# inside the archive as data/backups/.manifest.json rather than a separate file.
#
# What is backed up:
#   /data/batonkeep.db, /data/sessions/, /data/outputs/, /data/publish/, /data/*.json
#
# What is excluded:
#   /home/agent         — provider CLI OAuth tokens (re-auth after restore)
#   __pycache__, *.pyc  — regenerable Python bytecode
#
# Note: BYO API keys in batonkeep.db are encrypted with APP_SECRET. The same
# APP_SECRET value must be present in .env on the restore target — if it differs,
# stored API keys will fail to decrypt and must be re-entered in Settings.
#
# Exit codes: 0 success · 1 usage error · 3 output dir error · 4 tar failed

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
OUTPUT_DIR="/data/backups"
LABEL=""
DRY_RUN=false
STDOUT_MODE=false

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)  OUTPUT_DIR="$2"; shift 2 ;;
    -n|--name)    LABEL="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --stdout)     STDOUT_MODE=true; shift ;;
    -h|--help)    grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# In --stdout mode all user-facing messages go to stderr so the binary tar
# stream on stdout stays clean.
LOG() { echo "$@" >&2; }

# ── Archive name ──────────────────────────────────────────────────────────────
TS="$(date -u '+%Y%m%dT%H%M%SZ')"
if [[ -n "${LABEL}" ]]; then
  ARCHIVE_NAME="batonkeep-backup-${TS}-${LABEL}.tar.gz"
  MANIFEST_NAME="batonkeep-backup-${TS}-${LABEL}.manifest.json"
else
  ARCHIVE_NAME="batonkeep-backup-${TS}.tar.gz"
  MANIFEST_NAME="batonkeep-backup-${TS}.manifest.json"
fi

# ── What to back up ───────────────────────────────────────────────────────────
INCLUDE_PATHS=(
  "/data/batonkeep.db"
  "/data/sessions"
  "/data/outputs"
  "/data/publish"
  "/data/custom-providers.json"
  "/data/provider-instances.json"
  "/data/provider-enabled.json"
  "/data/exec-seam-overrides.json"
  "/data/model-catalog.json"
  "/data/model-overrides.json"
  "/data/model-pricing.json"
)
EXISTING_PATHS=()
for p in "${INCLUDE_PATHS[@]}"; do
  [[ -e "$p" ]] && EXISTING_PATHS+=("$p")
done

EXCLUDES=(
  # Node package trees — regenerable with npm/yarn/pnpm install.
  # node_modules alone was 1.6 GB (84%) of session data in testing.
  "*/node_modules"
  # Python virtual environments — regenerable with pip/uv install
  "*/.venv"
  "*/venv"
  # Download caches — always regenerable
  "*/.cache/uv"
  "*/.cache/pip"
  "*/.npm"
  # Python bytecode — always regenerable
  "*/__pycache__"
  "*.pyc"
  "*.pyo"
  # Next.js build cache (NOT dist — dist is kept as agent-produced output)
  "*/.next/cache"
  # Temp / lock files
  "*.sock"
  "*/.DS_Store"
)
EXCLUDE_FLAGS=()
for pat in "${EXCLUDES[@]}"; do
  EXCLUDE_FLAGS+=(--exclude="${pat}")
done

# ── Dry-run ───────────────────────────────────────────────────────────────────
if [[ "${DRY_RUN}" == true ]]; then
  echo "=== batonkeep-backup DRY RUN ==="
  if [[ "${STDOUT_MODE}" == true ]]; then
    echo "Mode: --stdout (archive would stream to fd 1)"
  else
    echo "Output: ${OUTPUT_DIR}/${ARCHIVE_NAME}"
  fi
  echo ""
  echo "--- Paths included ---"
  for p in "${EXISTING_PATHS[@]}"; do
    SIZE=""
    command -v du &>/dev/null && SIZE=" ($(du -sh "$p" 2>/dev/null | awk '{print $1}'))"
    echo "  $p$SIZE"
  done
  echo ""
  echo "--- Paths declared but absent (skipped) ---"
  for p in "${INCLUDE_PATHS[@]}"; do
    [[ ! -e "$p" ]] && echo "  $p"
  done
  echo ""
  echo "--- Exclusions ---"
  for pat in "${EXCLUDES[@]}"; do echo "  $pat"; done
  echo ""
  echo "NOT included: /home/agent (provider OAuth tokens — re-auth after restore)"
  echo ""
  echo "Dry run complete. No archive was created."
  exit 0
fi

# ── Build manifest JSON (written to a temp file, embedded in the archive) ─────
SESSION_COUNT=0
[[ -d /data/sessions ]] && SESSION_COUNT=$(find /data/sessions -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')

EXISTING_JSON=$(printf '"%s",' "${EXISTING_PATHS[@]}")
EXISTING_JSON="[${EXISTING_JSON%,}]"
EXCLUDE_JSON=$(printf '"%s",' "${EXCLUDES[@]}")
EXCLUDE_JSON="[${EXCLUDE_JSON%,}]"

MANIFEST_TMP="$(mktemp /tmp/batonkeep-manifest.XXXXXX.json)"
trap 'rm -f "${MANIFEST_TMP}"' EXIT

cat > "${MANIFEST_TMP}" <<EOF
{
  "schema": "batonkeep-backup-manifest/2",
  "created_at": "${TS}",
  "hostname": "$(hostname 2>/dev/null || echo container)",
  "label": "${LABEL}",
  "archive": "${ARCHIVE_NAME}",
  "included_paths": ${EXISTING_JSON},
  "exclusions": ${EXCLUDE_JSON},
  "session_count": ${SESSION_COUNT},
  "not_included": ["/home/agent (provider OAuth tokens — re-auth after restore)"],
  "restore_note": "APP_SECRET in .env must match backup-time value for BYO API key decryption"
}
EOF

# ── --stdout mode: stream tar directly to fd 1 ────────────────────────────────
if [[ "${STDOUT_MODE}" == true ]]; then
  LOG "=== batonkeep-backup (streaming to stdout) ==="
  LOG "Timestamp: ${TS}  Sessions: ${SESSION_COUNT}"
  LOG "Piping tar stream — redirect stdout to a .tar.gz file on the host."

  tar \
    "${EXCLUDE_FLAGS[@]}" \
    -cz \
    --transform "s|${MANIFEST_TMP#/}|data/backups/${MANIFEST_NAME}|" \
    "${EXISTING_PATHS[@]}" \
    "${MANIFEST_TMP}" \
    2>/dev/null

  TAR_EXIT=$?
  if [[ "${TAR_EXIT}" -ne 0 ]]; then
    LOG "ERROR: tar failed (exit ${TAR_EXIT})"
    exit 4
  fi
  LOG "Done. Restore with:"
  LOG "  cat <archive> | docker compose exec -T backend bash /app/scripts/batonkeep-restore.sh --stdin"
  exit 0
fi

# ── File mode: write archive to OUTPUT_DIR inside the container ───────────────
mkdir -p "${OUTPUT_DIR}" || { echo "ERROR: cannot create ${OUTPUT_DIR}" >&2; exit 3; }

ARCHIVE_PATH="${OUTPUT_DIR}/${ARCHIVE_NAME}"
MANIFEST_PATH="${OUTPUT_DIR}/${MANIFEST_NAME}"

# Copy manifest to its final location alongside the archive
cp "${MANIFEST_TMP}" "${MANIFEST_PATH}"

echo "Creating backup: ${ARCHIVE_PATH}"
echo "Sessions: ${SESSION_COUNT} workspace(s)"

tar \
  "${EXCLUDE_FLAGS[@]}" \
  --exclude="${ARCHIVE_PATH}" \
  --exclude="${MANIFEST_PATH}" \
  -czf "${ARCHIVE_PATH}" \
  "${EXISTING_PATHS[@]}" \
  2>&1 | grep -v "^tar: Removing leading" || true

TAR_EXIT=${PIPESTATUS[0]}
if [[ "${TAR_EXIT}" -ne 0 ]]; then
  echo "ERROR: tar failed (exit ${TAR_EXIT})" >&2
  rm -f "${ARCHIVE_PATH}" "${MANIFEST_PATH}"
  exit 4
fi

SIZE=$(du -sh "${ARCHIVE_PATH}" 2>/dev/null | awk '{print $1}')
echo ""
echo "=== Backup complete ==="
echo "Archive:  ${ARCHIVE_PATH}"
echo "Manifest: ${MANIFEST_PATH}"
echo "Size:     ${SIZE}"
echo ""
echo "Stream to host (no copy step):"
echo "  docker compose exec -T backend bash /app/scripts/batonkeep-backup.sh --stdout > backup.tar.gz"
