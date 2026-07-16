#!/usr/bin/env python3
"""verify_restore.py — verify a restored Batonkeep data directory (S0 substrate).

Run after `batonkeep-restore.sh`, pointing at the restored data root:

    docker compose exec backend python /app/scripts/verify_restore.py /data

Checks (stdlib only — safe to run before the app starts):
  • the database is present and opens;
  • every owner has exactly one default "Personal workspace" Project;
  • evidence integrity (sampled): each sampled Evidence row's file exists under
    <data>/evidence/project_<id>/ and its sha256 matches the recorded digest —
    the append-only store's digests are pinned at capture, so any drift here
    means the restore (or the source archive) corrupted evidence.

Exit code 0 = verified; 1 = problems found (each printed); 2 = usage error.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

DB_NAME = "batonkeep.db"
EVIDENCE_SAMPLE = 20


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def verify_restore(data_dir: str) -> list[str]:
    """Return a list of problems (empty = verified)."""
    problems: list[str] = []
    db_path = os.path.join(data_dir, DB_NAME)
    if not os.path.isfile(db_path):
        return [f"database missing: {db_path}"]

    conn = sqlite3.connect(db_path)
    try:
        # Pre-substrate databases have no projects table — nothing to verify
        # beyond the DB opening (older backups stay restorable).
        if _table_exists(conn, "projects") and _table_exists(conn, "owners"):
            owners = [r[0] for r in conn.execute("SELECT id FROM owners")]
            for owner_id in owners:
                n = conn.execute(
                    "SELECT COUNT(*) FROM projects WHERE owner_id=? AND is_default=1",
                    (owner_id,),
                ).fetchone()[0]
                if n != 1:
                    problems.append(
                        f"owner {owner_id!r}: expected exactly 1 default project, found {n}"
                    )

        if _table_exists(conn, "evidence"):
            rows = conn.execute(
                "SELECT id, project_id, rel_path, digest FROM evidence "
                "ORDER BY id DESC LIMIT ?",
                (EVIDENCE_SAMPLE,),
            ).fetchall()
            for ev_id, project_id, rel_path, digest in rows:
                path = os.path.join(data_dir, "evidence", f"project_{project_id}", rel_path)
                if not os.path.isfile(path):
                    problems.append(f"evidence {ev_id}: file missing ({rel_path})")
                    continue
                if digest:
                    with open(path, "rb") as f:
                        actual = hashlib.sha256(f.read()).hexdigest()
                    if actual != digest:
                        problems.append(
                            f"evidence {ev_id}: digest mismatch ({rel_path}) — "
                            f"recorded {digest[:12]}…, file {actual[:12]}…"
                        )
    finally:
        conn.close()
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    data_dir = os.path.abspath(sys.argv[1])
    if not os.path.isdir(data_dir):
        print(f"error: {data_dir} is not a directory")
        return 2
    problems = verify_restore(data_dir)
    if problems:
        print(f"[verify-restore] FAILED — {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"[verify-restore] OK — {data_dir} verified (default projects + evidence digests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
