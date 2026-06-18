#!/usr/bin/env bash
# batonkeep-restore — D-0050: restore a Batonkeep installation from a backup archive.
#
# Usage (from the directory containing docker-compose.yml):
#
#   # Stream directly from a host file — no copy step needed:
#   cat backup.tar.gz | docker compose exec -T backend bash /app/scripts/batonkeep-restore.sh --stdin
#
#   # Or from an archive already inside the container:
#   docker compose exec backend bash /app/scripts/batonkeep-restore.sh /data/backups/<archive>.tar.gz
#
# IMPORTANT — read before restoring:
#   1. Stop the stack first:  docker compose down
#   2. This script overwrites existing /data/* — run on a fresh volume or a
#      stopped stack only. It will not start or stop the application.
#   3. Provider credentials (/home/agent) are NOT in the archive; re-auth
#      after restore: docker compose exec -u sandbox -e HOME=/home/agent \
#        backend bash /app/scripts/auth.sh
#   4. Verify APP_SECRET in .env matches the backup-time value.
#      (BYO API keys in batonkeep.db are encrypted with APP_SECRET — a mismatch
#      means stored credentials can't be decrypted and must be re-entered.)
#
# Exit codes: 0 success · 1 usage error · 2 archive not found · 4 tar failed

set -euo pipefail

STDIN_MODE=false
ARCHIVE=""
TARGET="/data"
NON_INTERACTIVE=false

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stdin)       STDIN_MODE=true; shift ;;
    --target)      TARGET="$2"; shift 2 ;;
    --yes|-y)      NON_INTERACTIVE=true; shift ;;
    -h|--help)     grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0 ;;
    -*)            echo "Unknown option: $1" >&2; exit 1 ;;
    *)             ARCHIVE="$1"; shift ;;
  esac
done

if [[ "${STDIN_MODE}" == false && -z "${ARCHIVE}" ]]; then
  echo "Usage: batonkeep-restore.sh <archive.tar.gz>" >&2
  echo "       batonkeep-restore.sh --stdin              (pipe from host)" >&2
  exit 1
fi

if [[ "${STDIN_MODE}" == false && ! -f "${ARCHIVE}" ]]; then
  echo "ERROR: archive not found: ${ARCHIVE}" >&2
  exit 2
fi

# ── Pre-flight warning ────────────────────────────────────────────────────────
echo "=== batonkeep-restore ==="
if [[ "${STDIN_MODE}" == true ]]; then
  echo "Source:  stdin (streaming from host)"
else
  echo "Source:  ${ARCHIVE}"
fi
echo "Target:  ${TARGET}"
echo ""
echo "WARNING: This will overwrite existing files under ${TARGET}."
echo "         Ensure the stack is stopped (docker compose down) before proceeding."
echo ""

if [[ "${NON_INTERACTIVE}" == false ]]; then
  read -r -p "Continue? [y/N] " confirm
  if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    echo "Aborted."
    exit 0
  fi
  echo ""
fi

# ── Extract ───────────────────────────────────────────────────────────────────
echo "Extracting to ${TARGET}..."

# Archive contains paths like "data/batonkeep.db" (tar strips the leading /).
# Extracting to / restores them to /data/batonkeep.db.
# For a custom --target we remap the "data/" prefix.

_extract() {
  local src="$1"  # "-" for stdin, or a file path
  if [[ "${TARGET}" == "/data" ]]; then
    tar -xzf "${src}" -C / 2>&1 | grep -v "^tar: " || true
  else
    local rel="${TARGET#/}"
    tar -xzf "${src}" -C / \
      --transform "s|^data|${rel}|" \
      2>&1 | grep -v "^tar: " || true
  fi
}

if [[ "${STDIN_MODE}" == true ]]; then
  # --stdin: read the tar stream from fd 0.
  # We must buffer to a temp file because tar -xzf - with a pipe can fail if
  # the stream is not seekable (some tar implementations). Use a temp file
  # in /tmp (always writable by batond).
  TMPARCHIVE="$(mktemp /tmp/batonkeep-restore.XXXXXX.tar.gz)"
  trap 'rm -f "${TMPARCHIVE}"' EXIT
  echo "Buffering stdin stream..."
  cat > "${TMPARCHIVE}"
  echo "Stream buffered ($(du -sh "${TMPARCHIVE}" | awk '{print $1}')). Extracting..."
  _extract "${TMPARCHIVE}"
else
  _extract "${ARCHIVE}"
fi

TAR_EXIT=${PIPESTATUS[0]:-0}
if [[ "${TAR_EXIT}" -ne 0 ]]; then
  echo "ERROR: tar extraction failed (exit ${TAR_EXIT})" >&2
  exit 4
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Restore complete ==="
echo ""
echo "Next steps:"
echo "  1. Start the stack:  docker compose up -d"
echo "  2. Re-auth providers (credentials excluded from backup):"
echo "       docker compose exec -u sandbox -e HOME=/home/agent backend bash /app/scripts/auth.sh"
echo "  3. Verify APP_SECRET in .env matches the value from when the backup was made."
echo "     (BYO API keys are encrypted with APP_SECRET; a mismatch means stored"
echo "      credentials can't be decrypted and must be re-entered in Settings.)"
