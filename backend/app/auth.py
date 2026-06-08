"""
auth.py — Single-operator app-level authentication (D-0023, resolves P-0026).

When ``APP_PASSWORD`` is set, the whole API requires a logged-in session: a
signed, httpOnly cookie issued by ``POST /api/auth/login`` after a constant-time
password check. This protects the *data* (sessions, runs, artifacts, provider
config), not just the scoped console.

For personal/oss this also **folds in the old web-console token**: an
authenticated operator is trusted, so the scoped console is gated on
"authenticated & non-managed" instead of a separate ``WEB_CONSOLE_TOKEN`` (see
``Settings.console_actions_available``). Managed keeps its own platform-account
model (out of scope here) and the console exec-fence stays hard-off regardless
of login.

Signing uses stdlib ``hmac`` over ``APP_SECRET`` — no new dependency. The token
is ``<b64(payload)>.<b64(hmac)>`` where the payload is the issued-at unix time;
verification is constant-time and TTL-bounded.
"""
from __future__ import annotations

import base64
import hmac
import logging
import time
from hashlib import sha256

from app.config import get_settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "bk_session"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _signing_key() -> bytes:
    """Key material for cookie signing.

    Prefer ``APP_SECRET`` (the same secret that encrypts BYO credentials); fall
    back to ``APP_PASSWORD`` so a deployment that sets only the password still
    gets stable sessions across restarts. Both empty ⇒ app-auth is disabled and
    this is never called.
    """
    s = get_settings()
    material = s.app_secret or s.app_password
    return sha256(material.encode("utf-8")).digest()


def password_matches(candidate: str) -> bool:
    """Constant-time comparison of a login attempt against ``APP_PASSWORD``."""
    expected = get_settings().app_password
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def issue_session() -> str:
    """Mint a signed session token (payload = issued-at unix seconds)."""
    payload = str(int(time.time())).encode("ascii")
    sig = hmac.new(_signing_key(), payload, sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_session(token: str | None) -> bool:
    """True iff ``token`` is a valid, unexpired session cookie value."""
    if not token or "." not in token:
        return False
    settings = get_settings()
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        expected_sig = hmac.new(_signing_key(), payload, sha256).digest()
        if not hmac.compare_digest(_b64d(sig_b64), expected_sig):
            return False
        issued_at = int(payload.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return False
    age = time.time() - issued_at
    if age < 0 or age > settings.app_session_ttl_seconds:
        return False
    return True
