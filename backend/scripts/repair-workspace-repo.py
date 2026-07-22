#!/usr/bin/env python3
"""repair-workspace-repo.py — re-adopt a session workspace repo an agent displaced.

Before P-0079, `.git` was the one directory in a group-co-writable workspace the
`sandbox` agent could not write. An agent that ran `git` itself was denied, so it
renamed our repo aside (`.git_old` / `.git.old`) and ran its own — after which the
control plane could read neither and reported zero versions, no verified outputs,
and a packaging 409 for a workspace holding committed work.

P-0079 fixes the cause. This repairs workspaces already in that state.

**The adoption is gated on provable lineage.** Batonkeep must not attest to
history it never mediated, so this refuses to adopt unless the harness's own root
commit is an ancestor of the agent's HEAD — i.e. the agent cloned our repo before
displacing it, and its history is a strict superset of ours. Where that cannot be
shown, the workspace is left alone and reported, not "repaired" by guesswork.

Nothing is deleted. The agent's repo is adopted in place; the preserved original
stays as evidence under its existing name.

**Run it as the control-plane user** (`-u batond`). The whole classification is
"can the control plane read this repo *as itself*", so the answer depends on the
euid: as root, git rejects every `batond`-owned repo for dubious ownership and
each healthy workspace looks displaced. The script refuses to run as the wrong
user rather than report that.

    # inspect every session, change nothing (default)
    docker exec -u batond batonkeep-backend-1 python /app/scripts/repair-workspace-repo.py

    # repair the ones that pass the ancestry gate
    docker exec -u batond batonkeep-backend-1 python /app/scripts/repair-workspace-repo.py --apply

    # a specific session
    docker exec -u batond batonkeep-backend-1 python /app/scripts/repair-workspace-repo.py --apply --session <id>

Exit 0 when every inspected workspace is healthy or repaired, 1 when any needs a
human decision, 2 when it cannot run safely.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import subprocess
import sys
from datetime import datetime, timezone

SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "/data/sessions")
BACKUP_NAMES = (".git_old", ".git.old", ".git.bak", ".git-old")
OWNER = os.environ.get("BATOND_USER", "batond")
GROUP = os.environ.get("AGENTS_GROUP", "agents")


def git(repo_dir: str, *args: str, git_dir: str | None = None) -> tuple[int, str, str]:
    """Run git with a trust exception scoped to exactly this path (never `*`)."""
    target = git_dir or repo_dir
    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": os.path.realpath(target),
    }
    cmd = ["git"]
    cmd += ["--git-dir", target] if git_dir else ["-C", repo_dir]
    proc = subprocess.run([*cmd, *args], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def find_backup(workspace: str) -> str | None:
    for name in BACKUP_NAMES:
        path = os.path.join(workspace, name)
        if os.path.isdir(path):
            return path
    return None


def _username(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def owner_of(path: str) -> str:
    st = os.stat(path)
    try:
        user = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        user = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return f"{user}:{group}"


def inspect(workspace: str) -> dict:
    """Classify one workspace without changing anything."""
    out: dict = {"workspace": workspace, "session": os.path.basename(workspace)}
    git_dir = os.path.join(workspace, ".git")
    if not os.path.isdir(git_dir):
        return {**out, "state": "no-repo", "action": "none"}

    out["git_owner"] = owner_of(git_dir)
    # Deliberately *without* a trust exception: the question is whether the
    # control plane can read this repo as itself.
    probe = subprocess.run(["git", "-C", workspace, "rev-parse", "--git-dir"],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        return {**out, "state": "ok", "action": "none"}
    return classify_displaced(workspace, out)


def classify_displaced(workspace: str, out: dict | None = None) -> dict:
    """Decide whether an unreadable repo may be adopted. **This is the gate.**

    Separated from `inspect` so the safety property is testable: the
    dubious-ownership condition that triggers it needs two uids and cannot be
    reproduced in-process, but the ancestry decision can and must be.
    """
    out = {**(out or {}), "workspace": workspace,
           "session": out.get("session") if out else os.path.basename(workspace)}
    # Unreadable to us. Is it a displaced repo we can prove descends from ours?
    backup = find_backup(workspace)
    out["backup"] = backup
    if not backup:
        return {
            **out, "state": "foreign-no-original", "action": "manual",
            "why": "the agent's repo is unreadable and our original was not preserved; "
                   "nothing proves this history descends from ours",
        }

    rc_old, old_head, _ = git(workspace, "rev-parse", "HEAD", git_dir=backup)
    if rc_old != 0 or not old_head:
        return {**out, "state": "original-unreadable", "action": "manual",
                "why": f"cannot read HEAD of {backup}"}
    out["our_root"] = old_head

    rc_new, new_head, _ = git(workspace, "rev-parse", "HEAD")
    if rc_new != 0 or not new_head:
        return {**out, "state": "agent-repo-unreadable", "action": "manual",
                "why": "the displaced repo has no readable HEAD"}
    out["agent_head"] = new_head

    # THE GATE. Our commit must be reachable from theirs; anything else is a
    # foreign history and adopting it would be an attestation we cannot make.
    rc, _, _ = git(workspace, "merge-base", "--is-ancestor", old_head, new_head)
    if rc != 0:
        return {
            **out, "state": "foreign-disjoint", "action": "manual",
            "why": f"our root {old_head[:8]} is NOT an ancestor of the agent's HEAD "
                   f"{new_head[:8]} — this is a different history, not our own extended",
        }

    rc, count, _ = git(workspace, "rev-list", "--count", f"{old_head}..{new_head}")
    out["commits_ahead"] = int(count) if rc == 0 and count.isdigit() else None
    return {**out, "state": "adoptable", "action": "adopt"}


def adopt(entry: dict) -> dict:
    """Chown the repo back and share it with the agents group. Deletes nothing."""
    workspace = entry["workspace"]
    git_dir = os.path.join(workspace, ".git")
    try:
        uid = pwd.getpwnam(OWNER).pw_uid
    except KeyError:
        return {**entry, "applied": False, "error": f"user {OWNER!r} not found"}
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        gid = -1

    for dirpath, dirnames, filenames in os.walk(git_dir):
        for name in ("", *dirnames, *filenames):
            path = os.path.join(dirpath, name) if name else dirpath
            try:
                os.chown(path, uid, gid)
                mode = os.stat(path).st_mode & 0o7777
                want = mode | ((mode & 0o600) >> 3)
                if os.path.isdir(path):
                    want |= 0o2000 | (0o010 if mode & 0o100 else 0)
                if want != mode:
                    os.chmod(path, want)
            except OSError as exc:
                return {**entry, "applied": False, "error": f"{path}: {exc}"}
    try:
        os.chown(git_dir, uid, gid)
        os.chmod(git_dir, (os.stat(git_dir).st_mode & 0o7777) | 0o2070)
    except OSError as exc:
        return {**entry, "applied": False, "error": f"{git_dir}: {exc}"}

    git(workspace, "config", "core.sharedRepository", "group")
    # Re-read without the trust exception: the point is that we can now read it
    # as ourselves, which is the whole claim being repaired.
    proc = subprocess.run(["git", "-C", workspace, "log", "--oneline"],
                          capture_output=True, text=True)
    return {
        **entry,
        "applied": proc.returncode == 0,
        "readable_without_exception": proc.returncode == 0,
        "versions_visible": len(proc.stdout.strip().splitlines()) if proc.returncode == 0 else 0,
        "error": None if proc.returncode == 0 else proc.stderr.strip(),
    }


def boot_scan(root: str, report_path: str, *, owner_uid: int | None = None) -> list[dict]:
    """Classify displaced repos **before** the boot chown erases the evidence.

    `entrypoint.sh` re-owns `/data/sessions` to `batond:agents` on every start.
    That is what keeps a mixed-uid tree working, but it also adopts any repo an
    agent left behind — including one with no shared ancestry — and it does so
    silently. After it runs, a displaced repo is indistinguishable from ours.

    So the gate has to run first, and it cannot use the readability probe: this
    executes as root, where git rejects every `batond`-owned repo. Ownership is
    the signal here — a `.git` not owned by the control-plane user was written by
    an agent, and only a provable ancestor relationship makes it ours to adopt.

    Never blocks boot. A container that refuses to start because one session is
    odd trades a provenance error for an availability failure, which is worse.
    """
    # Explicit rather than resolved-only-internally: the whole classification
    # turns on "is this `.git` owned by the control plane", and a test in which
    # one user owns everything cannot exercise that unless the uid is injectable.
    if owner_uid is None:
        try:
            owner_uid = pwd.getpwnam(OWNER).pw_uid
        except KeyError:
            return []
    batond_uid = owner_uid

    flagged: list[dict] = []
    for sid in sorted(os.listdir(root)):
        workspace = os.path.join(root, sid)
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(workspace) or not os.path.isdir(git_dir):
            continue
        try:
            if os.stat(git_dir).st_uid == batond_uid:
                continue  # ours; the chown changes nothing about provenance
        except OSError:
            continue

        entry = {"session": sid, "workspace": workspace,
                 "git_owner": owner_of(git_dir),
                 "detected": datetime.now(timezone.utc).isoformat()}
        backup = find_backup(workspace)
        if not backup:
            entry.update(verdict="unprovable",
                         why="agent-written repo with no preserved original — nothing "
                             "ties this history to the session we created")
            flagged.append(entry)
            continue

        rc_old, old_head, _ = git(workspace, "rev-parse", "HEAD", git_dir=backup)
        rc_new, new_head, _ = git(workspace, "rev-parse", "HEAD")
        if rc_old != 0 or rc_new != 0:
            entry.update(verdict="unprovable", why="could not read both HEADs to compare")
            flagged.append(entry)
            continue
        entry.update(our_root=old_head, agent_head=new_head,
                     original=os.path.basename(backup))
        rc, _, _ = git(workspace, "merge-base", "--is-ancestor", old_head, new_head)
        if rc == 0:
            rc, count, _ = git(workspace, "rev-list", "--count", f"{old_head}..{new_head}")
            entry.update(verdict="descended",
                         commits_ahead=int(count) if rc == 0 and count.isdigit() else None)
        else:
            entry.update(verdict="disjoint",
                         why=f"our root {old_head[:8]} is not an ancestor of {new_head[:8]} — "
                             "the boot chown is about to make a foreign history authoritative")
        flagged.append(entry)

    if flagged:
        payload = {"scanned": datetime.now(timezone.utc).isoformat(),
                   "sessions_dir": root, "flagged": flagged}
        try:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.chmod(report_path, 0o640)
            try:
                os.chown(report_path, batond_uid, -1)
            except OSError:
                pass
        except OSError as exc:
            print(f"[repo-gate] could not write {report_path}: {exc}", file=sys.stderr)
    elif os.path.exists(report_path):
        # Nothing displaced this boot — clear a stale report rather than let it
        # keep accusing sessions that have since been resolved.
        try:
            os.remove(report_path)
        except OSError:
            pass
    return flagged


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="adopt repos that pass the ancestry gate (default: report only)")
    ap.add_argument("--session", help="one session id (default: every session)")
    ap.add_argument("--sessions-dir", default=SESSIONS_DIR)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--boot-scan", action="store_true",
                    help="classify displaced repos by ownership before the boot chown; "
                         "writes --report and never blocks startup")
    ap.add_argument("--report", default=os.environ.get(
        "REPO_PROVENANCE_REPORT", "/data/repo-provenance.json"))
    args = ap.parse_args()

    root = args.sessions_dir
    if not os.path.isdir(root):
        print(f"sessions dir not found: {root}", file=sys.stderr)
        return 2

    if args.boot_scan:
        flagged = boot_scan(root, args.report)
        for f in flagged:
            level = "WARN" if f["verdict"] == "descended" else "ALERT"
            print(f"[repo-gate] {level} {f['session']} — {f['verdict']}"
                  + (f": {f['why']}" if f.get("why") else
                     f" ({f.get('commits_ahead')} commits ahead of our root)"))
        if flagged:
            print(f"[repo-gate] {len(flagged)} workspace(s) recorded in {args.report}")
        return 0  # never block boot

    # Refuse to classify from the wrong vantage point. Every verdict here is
    # "can the control plane read this as itself", which is a property of the
    # euid — run as root, git rejects every batond-owned repo for dubious
    # ownership and healthy workspaces are reported as displaced. Observed: a
    # root-run dry run called 42 of 44 workspaces foreign.
    tree_uid = os.stat(root).st_uid
    if os.geteuid() != tree_uid:
        who = _username(os.geteuid())
        owner = _username(tree_uid)
        print(
            f"refusing to run as {who!r}: {root} is owned by {owner!r}, and every\n"
            f"verdict this script makes depends on reading repos as that user.\n"
            f"Re-run with:  docker exec -u {owner} <container> python {sys.argv[0]} ...",
            file=sys.stderr,
        )
        return 2

    ids = [args.session] if args.session else sorted(os.listdir(root))
    results = []
    for sid in ids:
        workspace = os.path.join(root, sid)
        if not os.path.isdir(workspace):
            continue
        entry = inspect(workspace)
        if args.apply and entry["action"] == "adopt":
            entry = adopt(entry)
        results.append(entry)

    stamp = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(json.dumps({"ts": stamp, "applied": args.apply, "results": results}, indent=2))
    else:
        print(f"# repair-workspace-repo {stamp} "
              f"({'APPLY' if args.apply else 'report only'})\n")
        for r in results:
            if r["state"] == "ok":
                continue
            print(f"{r['session']}  [{r['state']}]")
            if r.get("git_owner"):
                print(f"    .git owner     : {r['git_owner']}")
            if r.get("backup"):
                print(f"    our original   : {os.path.basename(r['backup'])} "
                      f"(root {r.get('our_root', '?')[:8]})")
            if r.get("commits_ahead") is not None:
                print(f"    agent commits  : {r['commits_ahead']} ahead of our root")
            if r.get("why"):
                print(f"    NEEDS A HUMAN  : {r['why']}")
            if "applied" in r:
                print(f"    adopted        : {r['applied']}"
                      + (f"  ({r['versions_visible']} versions now readable)"
                         if r.get("applied") else f"  error: {r.get('error')}"))
            print()
        healthy = sum(1 for r in results if r["state"] == "ok")
        manual = [r for r in results if r["action"] == "manual"]
        print(f"{len(results)} workspaces · {healthy} healthy · {len(manual)} need a decision")

    return 1 if any(r["action"] == "manual" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
