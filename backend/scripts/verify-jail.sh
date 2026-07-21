#!/usr/bin/env bash
# verify-jail.sh — prove the P-0072 workspace jail actually fences one session's
# agent off from another's workspace.
#
# This is the boundary test the proposal asked for, as something executable. It
# is deliberately shaped like production: both workspaces are `root:agents 2770`
# and the sandbox user is in `agents`, so **DAC would allow every access below**.
# Anything that gets denied is denied by Landlock and nothing else — which is the
# whole claim, since every session's agent runs as the same uid.
#
# Requires root (the helper drops privileges itself) and a Linux kernel with
# Landlock enabled. Skips loudly rather than failing when the kernel lacks it —
# the code being correct and the host being capable are different facts.
#
#   sudo backend/scripts/verify-jail.sh
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/sandbox-spawn.c"
TMP="$(mktemp -d)"
HELPER="$TMP/sandbox-spawn"
S1="$TMP/sessions/s1"
S2="$TMP/sessions/s2"
fail=0

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

note() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=1; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root"; exit 2; }

note "Building the helper"
cc -O2 -Wall -Wextra -o "$HELPER" "$SRC" || { echo "compile failed"; exit 1; }
chown root:root "$HELPER" && chmod 4755 "$HELPER"

"$HELPER" --jail-probe
case $? in
  0) ;;
  2) echo "FATAL: helper built WITHOUT landlock support — the image would ship unfenced"
     exit 1 ;;
  *) echo "::warning::SKIP — this kernel cannot enforce Landlock, so the jail is unproven here."
     echo "The helper compiled with support; run this on a Linux >= 5.13 host with landlock enabled."
     exit 0 ;;
esac

note "Setting up two workspaces in the production shape (root:agents 2770)"
getent group agents >/dev/null || groupadd agents
id -u sandbox >/dev/null 2>&1 || useradd -m -d /home/agent -G agents sandbox
usermod -aG agents sandbox
mkdir -p "$S1" "$S2" && chmod 755 "$TMP" "$TMP/sessions"
echo "confidential-s2-content" > "$S2/secret.txt"
chown -R root:agents "$TMP/sessions"
chmod 2770 "$S1" "$S2"
chmod 660 "$S2/secret.txt"

# Sanity: DAC alone permits the cross-session access. If this ever stops being
# true the rest of the script proves nothing, so assert it up front.
note "Baseline — without the jail, DAC allows cross-session access (the defect)"
if "$HELPER" -- sh -c "cat '$S2/secret.txt' >/dev/null 2>&1"; then
  ok "unjailed agent CAN read another session's file (defect reproduced)"
else
  bad "unjailed read was denied — setup is wrong, the jail test below proves nothing"
fi

run_jailed() { "$HELPER" --jail "$S1" -- sh -c "$1" >/dev/null 2>&1; }

note "With the jail applied"
if run_jailed "echo mine > '$S1/out.txt'"; then
  ok "CAN write inside its own workspace"
else
  bad "CANNOT write its own workspace — the jail is too tight, agents would break"
fi

if run_jailed "cat '$S1/out.txt'"; then
  ok "CAN read inside its own workspace"
else
  bad "CANNOT read its own workspace — the jail is too tight"
fi

if run_jailed "cat '$S2/secret.txt'"; then
  bad "CAN STILL READ another session's file — jail not enforced"
else
  ok "CANNOT read another session's file (the confidentiality half)"
fi

if run_jailed "echo planted > '$S2/OUTPUT.md'"; then
  bad "CAN STILL WRITE another session's tree — jail not enforced"
else
  ok "CANNOT write another session's tree (the pilot #43 failure)"
fi

if run_jailed "ls '$TMP/sessions'"; then
  bad "CAN STILL ENUMERATE other sessions"
else
  ok "CANNOT enumerate the sessions directory"
fi

note "The jail must not break ordinary work"
if run_jailed "cat /etc/hostname"; then
  ok "CAN read the system view (/etc)"
else
  bad "CANNOT read /etc — agents would break"
fi

if run_jailed "/usr/bin/env true"; then
  ok "CAN exec system binaries"
else
  bad "CANNOT exec system binaries — agents would break"
fi

if [ "$fail" -eq 0 ]; then
  note "Workspace jail verified."
else
  note "Workspace jail FAILED verification."
fi
exit "$fail"
