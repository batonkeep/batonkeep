"""
projects.py — S0 substrate: default-Project resolution.

The Project is the durable top-level unit of work; tasks and sessions always
belong to one. During the staged migration the DB columns stay nullable, so the
API resolves a missing project_id here — to the owner's single `is_default`
"Personal workspace" Project — and always writes it. Existing clients that never
send project_id therefore keep working unchanged.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project

DEFAULT_PROJECT_NAME = "Personal workspace"


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
