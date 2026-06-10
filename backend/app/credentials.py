"""
credentials.py — Encrypted BYO-key store (§8, §13).

Keys are symmetrically encrypted with APP_SECRET using Fernet.
If APP_SECRET is empty, keys are stored as plaintext (dev mode — warn loudly).

API:
    store_credential(db, owner_id, provider, api_key) -> Credential
    get_credential(db, owner_id, provider) -> str | None
    delete_credential(db, owner_id, provider) -> bool
    list_credentials(db, owner_id) -> list[CredentialOut]
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Credential

logger = logging.getLogger(__name__)


def _get_fernet():
    """Lazily build a Fernet key from APP_SECRET. Returns None in dev mode (empty secret)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning("[credentials] cryptography not installed; keys stored plaintext")
        return None

    secret = get_settings().app_secret
    if not secret:
        logger.warning("[credentials] APP_SECRET is empty — BYO keys stored plaintext (dev mode)")
        return None

    # Derive a 32-byte key from the secret using SHA-256, then base64url-encode → valid Fernet key
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    from cryptography.fernet import Fernet
    return Fernet(key)


def _encrypt(plaintext: str) -> str:
    fernet = _get_fernet()
    if fernet is None:
        return plaintext
    return fernet.encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    fernet = _get_fernet()
    if fernet is None:
        return ciphertext
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        logger.error("[credentials] failed to decrypt credential — APP_SECRET may have changed")
        raise


def _key_hint(api_key: str) -> str:
    """Non-secret last-4 fingerprint for display ('…wxyz'). Never the full key."""
    tail = api_key.strip()[-4:]
    return f"…{tail}" if tail else ""


async def store_credential(
    db: AsyncSession, owner_id: str, provider: str, api_key: str,
    label: str | None = None,
) -> Credential:
    """Encrypt and upsert a BYO key for owner+provider (provider may be an instance id)."""
    ciphertext = _encrypt(api_key)
    hint = _key_hint(api_key)

    result = await db.execute(
        select(Credential).where(
            Credential.owner_id == owner_id,
            Credential.provider == provider,
        )
    )
    cred = result.scalar_one_or_none()
    if cred:
        cred.ciphertext = ciphertext
        cred.key_hint = hint
        if label is not None:
            cred.label = label
    else:
        cred = Credential(
            owner_id=owner_id, provider=provider, ciphertext=ciphertext,
            label=label, key_hint=hint,
        )
        db.add(cred)

    await db.commit()
    await db.refresh(cred)
    logger.info("[credentials] stored key for owner=%s provider=%s", owner_id, provider)
    return cred


async def get_credential(db: AsyncSession, owner_id: str, provider: str) -> str | None:
    """Return the decrypted API key, or None if not set."""
    result = await db.execute(
        select(Credential).where(
            Credential.owner_id == owner_id,
            Credential.provider == provider,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        return None
    return _decrypt(cred.ciphertext)


async def delete_credential(db: AsyncSession, owner_id: str, provider: str) -> bool:
    """Delete a stored key. Returns True if it existed."""
    result = await db.execute(
        delete(Credential).where(
            Credential.owner_id == owner_id,
            Credential.provider == provider,
        ).returning(Credential.id)
    )
    await db.commit()
    deleted = result.scalar_one_or_none() is not None
    if deleted:
        logger.info("[credentials] deleted key for owner=%s provider=%s", owner_id, provider)
    return deleted


async def resolve_api_key(
    provider: str,
    env_key: str | None,
    owner_id: str | None = None,
) -> str | None:
    """
    Resolve the API key an executor should use for a provider, in priority order:
      1. A BYO key stored via the UI for this owner+provider (explicit user intent).
      2. The deployment env var named by the provider's `env_key` (hosted default).
      3. None — caller should surface a clear "no credentials" error.

    Opens its own DB session so executors don't need one threaded through.
    """
    oid = owner_id or get_settings().owner_id
    try:
        from app.db import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Credential).where(
                    Credential.owner_id == oid,
                    Credential.provider == provider,
                )
            )
            cred = result.scalar_one_or_none()
            if cred is not None:
                stored = _decrypt(cred.ciphertext)
                # Observability: record that this stored key was actually used.
                cred.last_used_at = datetime.now(UTC)
                await db.commit()
                return stored
    except Exception as exc:  # DB unavailable (e.g. unit context) — fall back to env
        logger.debug("[credentials] store lookup failed for %s: %s", provider, exc)

    if env_key:
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val
    return None


async def list_credentials(db: AsyncSession, owner_id: str) -> list[dict]:
    """Return provider names that have keys stored (no plaintext values)."""
    result = await db.execute(
        select(
            Credential.provider, Credential.label, Credential.key_hint,
            Credential.created_at, Credential.last_used_at,
        ).where(Credential.owner_id == owner_id)
    )
    return [
        {
            "provider": row.provider, "label": row.label, "key_hint": row.key_hint,
            "created_at": row.created_at, "last_used_at": row.last_used_at,
        }
        for row in result.all()
    ]


async def secrets_status(db: AsyncSession, owner_id: str) -> list[dict]:
    """
    The named secrets-management surface (P-0009 #3): for every key-backed provider
    template, report where its credential resolves from, in resolve_api_key order:
      - "stored": a BYO key is in the encrypted store (with hint + last-used).
      - "env":    no stored key, but the provider's deployment env var is set.
      - "missing": neither — the provider can't authenticate.
    Plan-CLI providers (no api key) and the local mock are omitted; they don't take
    a secret. Never returns any plaintext or ciphertext.
    """
    from app.providers.registry import effective_model, get_instance, list_providers

    # Stored keys for this owner, indexed by credential provider id.
    stored = {row["provider"]: row for row in await list_credentials(db, owner_id)}

    rows: list[dict] = []
    for pdef in list_providers():
        if pdef.kind not in ("openai_compatible", "anthropic") or not pdef.env_key:
            continue  # CLI/mock providers don't authenticate with a stored secret
        cred = stored.get(pdef.name)
        if cred is not None:
            source = "stored"
        elif os.environ.get(pdef.env_key):
            source = "env"
        else:
            source = "missing"
        # Effective model id, so the surface can both display and edit it.
        inst = get_instance(pdef.name)
        model = effective_model(inst, pdef) if inst is not None else pdef.model
        rows.append({
            "provider": pdef.name,
            "tier": pdef.tier,
            "kind": pdef.kind,
            "env_key": pdef.env_key,
            "local": pdef.local,
            "source": source,
            "key_hint": cred["key_hint"] if cred else None,
            "model": model,
            "last_used_at": cred["last_used_at"] if cred else None,
        })
    return rows
