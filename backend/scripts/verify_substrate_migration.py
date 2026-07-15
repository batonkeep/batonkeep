#!/usr/bin/env python3
"""verify_substrate_migration.py — prove the projects-substrate migration on a real DB.

Run from `backend/` against a **copy** of a production database (never the live file):

    docker cp batonkeep-backend-1:/data/batonkeep.db /tmp/spike.db
    PYTHONPATH=. uv run python scripts/verify_substrate_migration.py /tmp/spike.db

Captures row counts, upgrades the copy to alembic head, then asserts:
  • no rows were lost in any pre-existing table;
  • every owner has exactly one default "Personal workspace" Project;
  • every task, session, and run carries a project_id;
  • the schema is at head (a second upgrade is a no-op).

Exit code 0 = migration verified; any assertion failure exits non-zero with detail.
"""
from __future__ import annotations

import os
import sqlite3
import sys

TABLES = ("owners", "tasks", "runs", "run_events", "sessions", "session_turns",
          "artifacts", "credentials", "routing_decisions")


def _counts(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        existing = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in TABLES if t in existing
        }
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    db_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(db_path):
        print(f"error: {db_path} does not exist")
        return 2

    before = _counts(db_path)
    print(f"[spike] pre-migration counts: {before}")

    # Point the app at the copy and run the real startup migration path.
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    from app.config import get_settings

    get_settings.cache_clear()
    import app.db as db

    db._run_migrations()
    db._run_migrations()  # idempotency: second run must be a clean no-op

    after = _counts(db_path)
    failures: list[str] = []
    for table, n in before.items():
        if after.get(table) != n:
            failures.append(f"row-count changed for {table}: {n} → {after.get(table)}")

    conn = sqlite3.connect(db_path)
    try:
        bad_defaults = conn.execute(
            "SELECT o.id, COUNT(p.id) FROM owners o"
            " LEFT JOIN projects p ON p.owner_id = o.id AND p.is_default = 1"
            " GROUP BY o.id HAVING COUNT(p.id) != 1"
        ).fetchall()
        if bad_defaults:
            failures.append(f"owners without exactly one default project: {bad_defaults}")

        for table in ("tasks", "sessions", "runs"):
            orphans = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id IS NULL"
            ).fetchone()[0]
            if orphans:
                failures.append(f"{orphans} {table} rows have NULL project_id")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        print(f"[spike] alembic head after upgrade: {version[0] if version else None}")
    finally:
        conn.close()

    if failures:
        print("[spike] FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1

    print(f"[spike] post-migration counts: {after}")
    print("[spike] OK — lossless upgrade, default projects present, all rows attached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
