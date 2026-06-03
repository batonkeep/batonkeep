#!/usr/bin/env bash
# auth.sh — guided plan-CLI login walkthrough (run inside the backend container via `make auth`).
#
# Logs the official agent(s) into YOUR OWN subscription. Logins persist on the
# agent_home volume ($HOME) and self-refresh — auth once, run headless forever.
# This drives the OFFICIAL binary only; it never reads, stores, or forwards an
# OAuth token (the compliance bright line, PLAN.md §1/§4.1).
#
# Usage:
#   auth.sh                 # walk through all default providers (claude, grok, agy, codex)
#   auth.sh claude          # only Claude (default instance → default config dir)
#   auth.sh grok agy        # a subset, in the given order
#   auth.sh claude:work     # an EXTRA instance (Phase B) — logs into its own config dir
#   auth.sh all             # explicit "all" default providers
#   auth.sh -h | --help
#
# Extra instances ("template:slug") are looked up in the JSON file named by
# PROVIDER_INSTANCES_CONFIG (the same file the backend reads). The instance must be
# declared there with a `cli_config_dir`; this script exports that dir via the
# template's config-dir env var so the login writes into the per-account dir.
set -uo pipefail

ALL_PROVIDERS=(claude grok agy codex)

# Per-template config-dir override env var (verified against the binaries).
config_env_for() {
  case "$1" in
    claude) echo "CLAUDE_CONFIG_DIR" ;;
    codex)  echo "CODEX_HOME" ;;
    grok)   echo "GROK_HOME" ;;
    agy)    echo "GEMINI_DIR" ;;
    *)      echo "" ;;
  esac
}

# Look up cli_config_dir for an instance id in PROVIDER_INSTANCES_CONFIG.
# Exit codes: 0 found (prints dir), 3 no config file, 4 id not declared.
lookup_instance_dir() {
  python3 - "$1" <<'PY'
import json, os, sys
inst_id = sys.argv[1]
path = os.environ.get("PROVIDER_INSTANCES_CONFIG")
if not path or not os.path.exists(path):
    sys.exit(3)
try:
    data = json.load(open(path))
except Exception:
    sys.exit(3)
for e in data.get("instances", []):
    if e.get("id") == inst_id:
        print(e.get("cli_config_dir", ""))
        sys.exit(0)
sys.exit(4)
PY
}

usage() {
  cat <<EOF
Usage: auth.sh [provider|instance ...]
  Log plan-CLI provider(s) / extra instance(s) into your own subscription.
  Default providers: ${ALL_PROVIDERS[*]}   (default: all)
  Extra instances:   <template>:<slug> (e.g. claude:work) — must be declared in
                     \$PROVIDER_INSTANCES_CONFIG with a cli_config_dir.

Examples:
  auth.sh                 # all default providers
  auth.sh claude          # only Claude (default instance)
  auth.sh grok agy        # Grok then Antigravity
  auth.sh claude:work     # extra Claude account → its own config dir
  make auth               # all defaults (via Makefile)
  make auth p=claude      # only Claude (via Makefile)
  make auth p=claude:work # an extra instance (via Makefile)
EOF
}

# Per-provider login walkthrough. Falls back to manual-install hints if the
# binary isn't present in the image (subscription-gated betas).
run_provider() {
  local token="$1" template slug cfg_env cfg_dir
  template="${token%%:*}"   # part before ':' (or whole token if no ':')
  local key="$template"
  local label bin login hint
  case "$key" in
    claude)
      label="Claude (Anthropic Max/Pro)"; bin="claude"; login="claude /login"
      hint="npm i -g @anthropic-ai/claude-code   # then: claude /login" ;;
    grok)
      label="Grok (xAI SuperGrok)"; bin="grok"; login="grok login"
      hint="npm install -g @xai-official/grok   # then: grok login" ;;
    agy)
      label="Antigravity (Google / Gemini)"; bin="agy"; login="agy auth"
      hint="curl -fsSL https://antigravity.google/cli/install.sh | bash   # then: agy auth" ;;
    codex)
      label="Codex (OpenAI ChatGPT Plus)"; bin="codex"
      login="codex login --device-auth"   # device-auth prints a URL+code — works headless/SSH
      hint="curl -fsSL https://chatgpt.com/codex/install.sh | sh   # then: codex login --device-auth" ;;
    *)
      echo " ! unknown provider: '${key}' (valid: ${ALL_PROVIDERS[*]})"; return 1 ;;
  esac

  # Resolve an extra instance ("template:slug") to its per-account config dir.
  cfg_env=""; cfg_dir=""
  if [ "$token" != "$template" ]; then
    slug="${token#*:}"
    cfg_dir="$(lookup_instance_dir "$token")"; local rc=$?
    if [ "$rc" -eq 3 ]; then
      echo " ! instance '${token}' requested but PROVIDER_INSTANCES_CONFIG is unset or missing."
      echo "   Declare it (see backend/config/provider-instances.example.json) and set the env var."
      return 1
    elif [ "$rc" -eq 4 ]; then
      echo " ! instance '${token}' is not declared in \$PROVIDER_INSTANCES_CONFIG."
      return 1
    elif [ -z "$cfg_dir" ]; then
      echo " ! instance '${token}' has no cli_config_dir in \$PROVIDER_INSTANCES_CONFIG."
      return 1
    fi
    cfg_env="$(config_env_for "$template")"
    label="${label} · instance ${token}"
  fi

  echo "------------------------------------------------------------"
  echo " ${label}"
  echo "------------------------------------------------------------"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo " '${bin}' is not installed in this image (subscription-gated beta)."
    echo " Install it manually, then re-run:"
    echo "     ${hint}"
    echo
    return 0
  fi

  echo " Found '${bin}'. Launching its login flow."
  echo " Follow the printed URL / device code in a browser, then return here."
  if [ -n "$cfg_dir" ]; then
    echo " Account config dir: ${cfg_env}=${cfg_dir}"
    mkdir -p "$cfg_dir"
    echo
    # Scope the env var to a subshell so it only affects this account's login.
    # shellcheck disable=SC2086
    ( export "${cfg_env}=${cfg_dir}"; $login ) || echo " ! '${bin}' login exited non-zero — re-run to retry."
  else
    echo
    # shellcheck disable=SC2086
    $login || echo " ! '${bin}' login exited non-zero — re-run to retry."
  fi
  echo
}

# ── Parse args → selected providers ─────────────────────────────────────────
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

if [ "$#" -eq 0 ] || [ "${1:-}" = "all" ]; then
  SELECTED=("${ALL_PROVIDERS[@]}")
else
  SELECTED=("$@")
fi

# Validate up front so a typo doesn't half-run. Instance ids validate on their
# template part (the slug is checked against the config inside run_provider).
for p in "${SELECTED[@]}"; do
  t="${p%%:*}"
  case " ${ALL_PROVIDERS[*]} " in
    *" $t "*) ;;
    *) echo "Unknown provider/template: '$p'"; echo; usage; exit 2 ;;
  esac
done

echo "============================================================"
echo " batonkeep · plan-CLI login walkthrough"
echo " HOME=$HOME  (persisted on the agent_home volume)"
echo " targets: ${SELECTED[*]}"
echo "============================================================"
echo

for p in "${SELECTED[@]}"; do
  run_provider "$p"
done

echo "============================================================"
echo " Done. Verify logins are healthy:"
echo "     curl -s localhost:8000/api/providers | python -m json.tool"
echo
echo " Then point tasks at your plans: set DEFAULT_CANDIDATES (e.g. claude,grok,agy)"
echo " in .env (or edit per-task routing in the UI) and 'make up' to reload."
echo "============================================================"
