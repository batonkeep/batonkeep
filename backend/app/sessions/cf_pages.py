"""
sessions/cf_pages.py — Cloudflare Pages publish connector (D-0009 host connector).

A third delivery mechanism alongside download-pack (#1) and the revocable backend
share link (#2): deploy a session's built static bundle to the user's Cloudflare
Pages project.

Security boundary (per the D-0012 discussion): this is a **backend-side** connector.
The Cloudflare API token is a high-privilege deploy credential — it lives in the
encrypted credential store and is handed only to a trusted `wrangler` subprocess
that operates on an already-built bundle. It is **never** injected into an agent
sandbox, so agent-authored code / prompt injection can't read or abuse it. The
account id + project name are config (not secrets) but ride along in the same
encrypted blob for simplicity.

Deploy is delegated to Cloudflare's official `wrangler pages deploy` rather than a
hand-rolled Direct-Upload client, so the upload protocol stays correct across
Cloudflare changes (founder call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app import credentials as creds
from app.sessions import publish as pub
from app.sessions import workspace as ws

logger = logging.getLogger(__name__)

# Stored as one encrypted credential row under this provider key (value = JSON below).
CF_PROVIDER = "cloudflare"
# Cloudflare project names: lowercase alnum + hyphens, ≤ 58 chars.
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,57}$")
# pages.dev deployment URL printed by wrangler on success.
_URL_RE = re.compile(r"https://[a-z0-9-]+\.[a-z0-9-]+\.pages\.dev", re.IGNORECASE)
_DEPLOY_TIMEOUT_S = 180


class CloudflareError(Exception):
    """Raised with a user-facing message when config is missing or a deploy fails."""


# ── Config storage (encrypted credential store) ───────────────────────────────

async def set_config(
    db: AsyncSession, owner_id: str, *, api_token: str, account_id: str, project_name: str
) -> None:
    """Validate + store the Cloudflare connector config (token encrypted at rest)."""
    api_token = (api_token or "").strip()
    account_id = (account_id or "").strip()
    project_name = (project_name or "").strip().lower()
    if not api_token or not account_id or not project_name:
        raise CloudflareError("api_token, account_id and project_name are all required")
    if not _PROJECT_RE.match(project_name):
        raise CloudflareError(
            "project_name must be lowercase letters, digits and hyphens (≤ 58 chars)"
        )
    blob = json.dumps(
        {"api_token": api_token, "account_id": account_id, "project_name": project_name}
    )
    await creds.store_credential(db, owner_id, CF_PROVIDER, blob, label="Cloudflare Pages")


async def get_config(db: AsyncSession, owner_id: str) -> Optional[dict]:
    """Return the decrypted config dict, or None if the connector isn't set up."""
    raw = await creds.get_credential(db, owner_id, CF_PROVIDER)
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        raise CloudflareError("stored Cloudflare config is corrupt — re-enter it")
    if not all(cfg.get(k) for k in ("api_token", "account_id", "project_name")):
        raise CloudflareError("stored Cloudflare config is incomplete — re-enter it")
    return cfg


async def clear_config(db: AsyncSession, owner_id: str) -> bool:
    """Remove the connector config. Returns True if it existed."""
    return await creds.delete_credential(db, owner_id, CF_PROVIDER)


# ── Deploy ────────────────────────────────────────────────────────────────────

def _materialize_bundle(workspace: str) -> str:
    """Copy the workspace's publishable static assets into a fresh temp dir."""
    out = tempfile.mkdtemp(prefix="cfpages-")
    for rel in pub._publishable_files(workspace):
        src = ws.safe_join(workspace, rel)
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return out


def _deploy_env(api_token: str, account_id: str) -> dict:
    """Env for wrangler: credentials passed via env, never on the command line."""
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = api_token
    env["CLOUDFLARE_ACCOUNT_ID"] = account_id
    env["WRANGLER_SEND_METRICS"] = "false"  # don't phone home from our backend
    return env


def _project_create_cmd(project_name: str, branch: str) -> list[str]:
    return ["wrangler", "pages", "project", "create", project_name,
            f"--production-branch={branch}"]


def _deploy_cmd(directory: str, project_name: str, branch: str) -> list[str]:
    return ["wrangler", "pages", "deploy", directory,
            f"--project-name={project_name}", f"--branch={branch}", "--commit-dirty=true"]


def _parse_deploy_url(output: str) -> Optional[str]:
    m = _URL_RE.search(output or "")
    return m.group(0) if m else None


async def _run(cmd: list[str], env: dict) -> tuple[int, str]:
    """Run a wrangler command; return (returncode, combined stdout+stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_DEPLOY_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise CloudflareError("Cloudflare deploy timed out")
    return proc.returncode, (out or b"").decode("utf-8", errors="replace")


async def deploy(workspace: str, config: dict, *, branch: str = "main") -> dict:
    """
    Deploy the workspace's current static build to Cloudflare Pages.

    Returns {"url": <deployment url>, "project": <name>}. Raises CloudflareError
    with the wrangler output on failure.
    """
    if not shutil.which("wrangler"):
        raise CloudflareError(
            "wrangler is not installed on the backend — Cloudflare publish unavailable"
        )
    project = config["project_name"]
    env = _deploy_env(config["api_token"], config["account_id"])
    directory = _materialize_bundle(workspace)
    if not os.listdir(directory):
        shutil.rmtree(directory, ignore_errors=True)
        raise CloudflareError("nothing to publish — the build has no files yet")
    try:
        # Idempotent: create the project if it doesn't exist; ignore "already exists".
        rc, out = await _run(_project_create_cmd(project, branch), env)
        if rc != 0 and "already exists" not in out.lower():
            logger.info("[cf_pages] project create non-fatal (rc=%s): %s", rc, out[-500:])

        rc, out = await _run(_deploy_cmd(directory, project, branch), env)
        if rc != 0:
            raise CloudflareError(f"Cloudflare deploy failed:\n{out[-1000:]}")
        url = _parse_deploy_url(out)
        if not url:
            raise CloudflareError(f"deploy reported success but no URL was found:\n{out[-500:]}")
        logger.info("[cf_pages] deployed project=%s url=%s", project, url)
        return {"url": url, "project": project}
    finally:
        shutil.rmtree(directory, ignore_errors=True)
