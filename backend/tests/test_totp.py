"""
tests/test_totp.py — TOTP second factor on the app login gate (D-0056, resolves P-0062).

Covers the RFC-6238 primitives (RFC 4226/6238 published test vectors), the
verify window + replay guard, the enrollment lifecycle (setup → activate →
login requires a code → disable), the TOTP_DISABLED break-glass, and the
middleware boundary (management routes are NOT public even though they live
under /api/auth/).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import totp
from app.config import get_settings

# RFC 4226 appendix D secret ("12345678901234567890") in base32.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.fixture
def settings_env():
    s = get_settings()
    # main.py binds `settings = get_settings()` at import; a prior test's
    # cache_clear (test_db_migrations) can leave that binding pointing at a
    # stale object. Re-converge it so endpoint code sees our mutations.
    saved_binding = main.settings
    main.settings = s
    fields = (
        "app_secret", "app_password", "app_session_ttl_seconds",
        "totp_disabled", "cookie_secure",
    )
    saved = {f: getattr(s, f) for f in fields}
    s.app_secret = "unit-test-secret"
    s.app_password = "s3cret"
    s.totp_disabled = False
    totp.throttle_reset()
    try:
        yield s
    finally:
        main.settings = saved_binding
        for f, v in saved.items():
            setattr(s, f, v)
        totp.throttle_reset()


@pytest.fixture
def client(settings_env, tmp_path):
    """TestClient over a fresh sqlite DB with the local owner seeded."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import Base, get_db
    from app.models import Owner

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/totp.db")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Maker = async_sessionmaker(engine, expire_on_commit=False)
        async with Maker() as db:
            db.add(Owner(id="local", label="Me"))
            await db.commit()
        return Maker

    Maker = asyncio.get_event_loop().run_until_complete(_setup())

    async def _override_db():
        async with Maker() as db:
            yield db

    main.app.dependency_overrides[get_db] = _override_db
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def _login(c, password="s3cret", code=None):
    body = {"password": password}
    if code is not None:
        body["totp_code"] = code
    return c.post("/api/auth/login", json=body)


@pytest.fixture
def clock(monkeypatch):
    """Deterministic clock driving app.totp's view of time; .tick() = next step."""
    class Clock:
        t = 1_000_000_000.0

        def tick(self, steps=1):
            self.t += steps * totp.STEP_SECONDS

        def code(self, secret):
            return totp.hotp(secret, int(self.t // totp.STEP_SECONDS))

    c = Clock()
    monkeypatch.setattr("app.totp.time.time", lambda: c.t)
    return c


# ── RFC vectors + verify semantics ───────────────────────────────────────────

def test_hotp_rfc4226_vectors():
    # RFC 4226 appendix D, 6-digit truncation.
    expected = ["755224", "287082", "359152", "969429", "338314"]
    assert [totp.hotp(RFC_SECRET, i) for i in range(5)] == expected


def test_totp_rfc6238_epoch():
    # RFC 6238 appendix B: T=59s → step 1 → HOTP(1); 6-digit form.
    assert totp.hotp(RFC_SECRET, 59 // 30) == "287082"


def test_verify_window_and_replay():
    now = 90.0  # step 3
    code_now = totp.hotp(RFC_SECRET, 3)
    code_prev = totp.hotp(RFC_SECRET, 2)
    code_old = totp.hotp(RFC_SECRET, 0)

    assert totp.verify_code(RFC_SECRET, code_now, last_counter=0, now=now) == 3
    # ±1 step tolerated (clock skew), older steps rejected.
    assert totp.verify_code(RFC_SECRET, code_prev, last_counter=0, now=now) == 2
    assert totp.verify_code(RFC_SECRET, code_old, last_counter=0, now=now) is None
    # Replay guard: a consumed counter never verifies again.
    assert totp.verify_code(RFC_SECRET, code_now, last_counter=3, now=now) is None
    # Malformed input.
    assert totp.verify_code(RFC_SECRET, "abc", last_counter=0, now=now) is None
    assert totp.verify_code(RFC_SECRET, "12345", last_counter=0, now=now) is None


# ── Enrollment lifecycle over the API ────────────────────────────────────────

def test_totp_routes_not_public(settings_env, client):
    # Management routes sit behind the session gate despite the /api/auth/ path.
    assert client.get("/api/auth/totp").status_code == 401
    assert client.post("/api/auth/totp/setup").status_code == 401
    # The login-gate trio stays public.
    assert client.get("/api/auth/status").status_code == 200


def test_full_lifecycle(settings_env, client, clock):
    c = client
    assert _login(c).status_code == 200  # password-only before enrollment

    # Enroll: setup returns the secret + otpauth URI; status flips to pending.
    r = c.post("/api/auth/totp/setup")
    assert r.status_code == 200
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert secret in r.json()["otpauth_uri"]
    st = c.get("/api/auth/totp").json()
    assert st == {"enabled": False, "pending": True, "break_glass": False}

    # Pending enrollment does NOT yet gate login (activation is the switch).
    assert c.get("/api/auth/status").json()["totp_enabled"] is False

    # Activate with a live code.
    assert c.post("/api/auth/totp/activate", json={"code": "000000"}).status_code == 401
    r = c.post("/api/auth/totp/activate", json={"code": clock.code(secret)})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert c.get("/api/auth/status").json()["totp_enabled"] is True

    # Fresh client: password alone now fails; password + a fresh-step code works
    # (activation consumed the current step, so advance one).
    c2 = TestClient(main.app)
    assert _login(c2).status_code == 401
    assert _login(c2, code="000000").status_code == 401
    clock.tick()
    used = clock.code(secret)
    assert _login(c2, code=used).status_code == 200

    # Replay: the same code (same step) can't log in twice.
    assert _login(TestClient(main.app), code=used).status_code == 401

    # Disable needs a valid current code; then password-only works again.
    assert c2.post("/api/auth/totp/disable", json={"code": "000000"}).status_code == 401
    clock.tick()
    r = c2.post("/api/auth/totp/disable", json={"code": clock.code(secret)})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert _login(TestClient(main.app)).status_code == 200


def test_setup_conflicts_when_active(settings_env, client, clock):
    c = client
    _login(c)
    secret = c.post("/api/auth/totp/setup").json()["secret"]
    c.post("/api/auth/totp/activate", json={"code": clock.code(secret)})
    # Active enrollment can't be silently replaced.
    assert c.post("/api/auth/totp/setup").status_code == 409


def test_break_glass_env(settings_env, client, clock):
    c = client
    _login(c)
    secret = c.post("/api/auth/totp/setup").json()["secret"]
    c.post("/api/auth/totp/activate", json={"code": clock.code(secret)})

    settings_env.totp_disabled = True
    # Password-only login works again; status reports the skip.
    c2 = TestClient(main.app)
    assert _login(c2).status_code == 200
    assert c2.get("/api/auth/status").json()["totp_enabled"] is False
    assert c2.get("/api/auth/totp").json()["break_glass"] is True


def test_requires_app_auth(settings_env, client):
    settings_env.app_password = ""
    assert client.post("/api/auth/totp/setup").status_code == 400


def test_throttle(settings_env, client, clock):
    c = client
    _login(c)
    secret = c.post("/api/auth/totp/setup").json()["secret"]
    c.post("/api/auth/totp/activate", json={"code": clock.code(secret)})

    c2 = TestClient(main.app)
    for _ in range(5):
        assert _login(c2, code="000000").status_code == 401
    # Sixth attempt hits the lockout window, even with a correct code shape.
    assert _login(c2, code="000000").status_code == 429
