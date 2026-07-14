"""
totp.py — Optional TOTP second factor on the app login gate (D-0056, resolves P-0062).

RFC-6238 TOTP over stdlib ``hmac`` — same no-new-dependency posture as auth.py.
Only meaningful when app-auth is enabled (``APP_PASSWORD`` set): once enrolled
and activated, ``POST /api/auth/login`` requires a valid 6-digit code alongside
the password.

State lives in the encrypted credential store (``credentials.py``) under the
reserved provider name ``_app_totp`` as a JSON blob:
``{"secret": <base32>, "activated": bool, "last_counter": int}``.
``last_counter`` is the replay guard — a code (time-step) is accepted at most
once, and only counters strictly greater than the last accepted one verify.

Recovery is env break-glass only (D-0056): the self-hosted operator sets
``TOTP_DISABLED=1`` (or deletes the stored secret row) to clear a lockout.
Enrolling does not touch existing sessions or the cookie TTL.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets as _secrets
import struct
import time
from hashlib import sha1
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app import credentials

logger = logging.getLogger(__name__)

# Reserved credential-store key; underscore-prefixed so it can never collide
# with a provider name (secrets_status only surfaces registry providers).
TOTP_PROVIDER_KEY = "_app_totp"

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # accept the current step ±1 (clock skew)
ISSUER = "Batonkeep"
ACCOUNT = "operator"


# ── RFC 4226 / 6238 primitives ──────────────────────────────────────────────

def generate_secret() -> str:
    """160-bit random secret, base32 (the standard authenticator-app format)."""
    return base64.b32encode(_secrets.token_bytes(20)).decode("ascii")


def hotp(secret_b32: str, counter: int) -> str:
    """RFC 4226 HOTP: HMAC-SHA1 + dynamic truncation → 6 digits."""
    key = base64.b32decode(secret_b32, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** DIGITS).zfill(DIGITS)


def verify_code(
    secret_b32: str, code: str, last_counter: int, now: float | None = None
) -> int | None:
    """Return the matched time-step counter, or None.

    Constant-time digit comparison; only counters strictly greater than
    ``last_counter`` are accepted (replay guard), within ±WINDOW steps of now.
    """
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    for counter in range(current - WINDOW, current + WINDOW + 1):
        if counter <= last_counter:
            continue
        if hmac.compare_digest(hotp(secret_b32, counter), code):
            return counter
    return None


def provisioning_uri(secret_b32: str) -> str:
    """otpauth:// URI for QR/manual enrollment in any authenticator app."""
    label = quote(f"{ISSUER}:{ACCOUNT}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


# ── Persistence over the encrypted credential store ─────────────────────────

async def load_state(db: AsyncSession, owner_id: str) -> dict | None:
    """The stored TOTP blob, or None when never enrolled."""
    raw = await credentials.get_credential(db, owner_id, TOTP_PROVIDER_KEY)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("[totp] stored state is not valid JSON — treating as unenrolled")
        return None


async def save_state(db: AsyncSession, owner_id: str, state: dict) -> None:
    await credentials.store_credential(
        db, owner_id, TOTP_PROVIDER_KEY, json.dumps(state), label="app TOTP (D-0056)",
    )


async def clear_state(db: AsyncSession, owner_id: str) -> bool:
    return await credentials.delete_credential(db, owner_id, TOTP_PROVIDER_KEY)


async def is_active(db: AsyncSession, owner_id: str) -> bool:
    """True iff TOTP is enrolled AND activated (a live code was verified once)."""
    state = await load_state(db, owner_id)
    return bool(state and state.get("activated"))


# ── Brute-force throttle (in-memory, single-operator scale) ─────────────────

_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 60
_failures: list[float] = []


def throttle_check() -> bool:
    """True when another code attempt is allowed right now."""
    cutoff = time.time() - _LOCKOUT_SECONDS
    while _failures and _failures[0] < cutoff:
        _failures.pop(0)
    return len(_failures) < _MAX_FAILURES


def throttle_record_failure() -> None:
    _failures.append(time.time())


def throttle_reset() -> None:
    _failures.clear()
