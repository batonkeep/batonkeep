"""
projects.py — S0 substrate: default-Project resolution + managed context roots.

The Project is the durable top-level unit of work; tasks and sessions always
belong to one. During the staged migration the DB columns stay nullable, so the
API resolves a missing project_id here — to the owner's single `is_default`
"Personal workspace" Project — and always writes it. Existing clients that never
send project_id therefore keep working unchanged.
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, WorkItem
from app.project_context import MANIFEST_API_VERSION, MANIFEST_KIND

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_NAME = "Personal workspace"


def init_managed_root(root: str, name: str, description: str | None) -> None:
    """Create a server-managed context root (S0.4): the directory, a starter
    README.md + batonkeep.yaml (bootstrap: README.md), and a git repo with an
    initial commit. Raises OSError when the base isn't writable — the route maps
    it to 409, mirroring the canonical-write posture. A missing git binary
    degrades to an un-versioned root with a warning: projection hashes file
    content directly, so git here is provenance, not a requirement."""
    os.makedirs(root, exist_ok=True)

    readme = os.path.join(root, "README.md")
    if not os.path.exists(readme):
        body = f"# {name}\n"
        if description:
            body += f"\n{description}\n"
        body += (
            "\nCanonical context root for this Batonkeep project. Files declared in\n"
            "`batonkeep.yaml` are projected read-only into every run's workspace;\n"
            "agent edits come back as proposals and land here only on approval.\n"
        )
        with open(readme, "w", encoding="utf-8") as f:
            f.write(body)

    manifest = os.path.join(root, "batonkeep.yaml")
    if not os.path.exists(manifest):
        doc: dict = {
            "apiVersion": MANIFEST_API_VERSION,
            "kind": MANIFEST_KIND,
            "name": name,
        }
        if description:
            doc["description"] = description
        doc["context"] = {"bootstrap": ["README.md"]}
        with open(manifest, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)

    if not os.path.isdir(os.path.join(root, ".git")):
        try:
            subprocess.run(
                ["git", "init", "-q", root],
                capture_output=True, text=True, timeout=15, check=True,
            )
            subprocess.run(
                ["git", "-C", root, "add", "-A"],
                capture_output=True, text=True, timeout=15, check=True,
            )
            subprocess.run(
                ["git", "-C", root,
                 # Committer identity inline — a host/container without a global
                 # git identity must not fail root creation.
                 "-c", "user.name=batonkeep", "-c", "user.email=noreply@batonkeep.local",
                 "commit", "-q", "-m", "init: managed context root",
                 "--author", "batonkeep <noreply@batonkeep.local>"],
                capture_output=True, text=True, timeout=15, check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("managed context root created but git init failed: %s", exc)


async def get_or_create_default_project(db: AsyncSession, owner_id: str) -> Project:
    """Return the owner's default Project, creating it if missing.

    The backfill migration creates one for every existing owner; this covers
    owners created after the migration (and fresh test databases built from
    metadata rather than migrations).
    """
    result = await db.execute(
        select(Project).where(Project.owner_id == owner_id, Project.is_default.is_(True))
    )
    project = result.scalars().first()
    if project is None:
        project = Project(
            id=uuid.uuid4().hex,
            owner_id=owner_id,
            name=DEFAULT_PROJECT_NAME,
            is_default=True,
        )
        db.add(project)
        await db.flush()
    return project


async def resolve_project_id(
    db: AsyncSession, owner_id: str, requested: str | None
) -> str:
    """Resolve the project a new task/session belongs to.

    None → the owner's default Project (created on demand). An explicit id must
    exist and belong to the owner; otherwise ValueError (the route maps it to 404
    — indistinguishable from absent, so foreign ids don't leak existence).
    """
    if requested is None:
        return (await get_or_create_default_project(db, owner_id)).id
    project = await db.get(Project, requested)
    if project is None or project.owner_id != owner_id:
        raise ValueError(f"Project {requested!r} not found")
    return project.id


async def resolve_work_item_id(
    db: AsyncSession, owner_id: str, project_id: str, requested: int | None
) -> int | None:
    """Validate an optional WorkItem attachment on task/session create.

    It must exist, belong to the owner, and live in the resolved project —
    otherwise ValueError (mapped to 404 by the route, same non-leaking posture
    as resolve_project_id)."""
    if requested is None:
        return None
    work_item = await db.get(WorkItem, requested)
    if (
        work_item is None
        or work_item.owner_id != owner_id
        or work_item.project_id != project_id
    ):
        raise ValueError(f"Work item {requested!r} not found")
    return work_item.id
