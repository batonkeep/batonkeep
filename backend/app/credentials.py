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
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

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


async def store_credential(
    db: AsyncSession, owner_id: str, provider: str, api_key: str,
    label: Optional[str] = None,
) -> Credential:
    """Encrypt and upsert a BYO key for owner+provider (provider may be an instance id)."""
    ciphertext = _encrypt(api_key)

    result = await db.execute(
        select(Credential).where(
            Credential.owner_id == owner_id,
            Credential.provider == provider,
        )
    )
    cred = result.scalar_one_or_none()
    if cred:
        cred.ciphertext = ciphertext
        if label is not None:
            cred.label = label
    else:
        cred = Credential(owner_id=owner_id, provider=provider, ciphertext=ciphertext, label=label)
        db.add(cred)

    await db.commit()
    await db.refresh(cred)
    logger.info("[credentials] stored key for owner=%s provider=%s", owner_id, provider)
    return cred


async def get_credential(db: AsyncSession, owner_id: str, provider: str) -> Optional[str]:
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
    env_key: Optional[str],
    owner_id: Optional[str] = None,
) -> Optional[str]:
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
            stored = await get_credential(db, oid, provider)
            if stored:
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
        select(Credential.provider, Credential.label, Credential.created_at).where(
            Credential.owner_id == owner_id
        )
    )
    return [
        {"provider": row.provider, "label": row.label, "created_at": row.created_at}
        for row in result.all()
    ]
