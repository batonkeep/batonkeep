"""
tests/test_auth.py — App-level auth (D-0023, resolves P-0026).

Covers the single-operator login gate: the signed-cookie session round-trip,
the enforcement middleware (protected routes 401 without a session; public
surfaces stay open), login/logout, and the web-console token fold-in
(authenticated operator is trusted; legacy token no longer required).
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main
from app.config import get_settings


@pytest.fixture
def settings_env():
    """Mutate the shared settings singleton (same object main.settings binds),
    snapshotting every field these tests touch so nothing leaks into other tests."""
    s = get_settings()
    fields = (
        "app_secret", "app_password", "app_session_ttl_seconds",
        "enable_web_console", "web_console_token", "cookie_secure",
    )
    saved = {f: getattr(s, f) for f in fields}
    s.app_secret = "unit-test-secret"
    try:
        yield s
    finally:
        for f, v in saved.items():
            setattr(s, f, v)


def _enable_auth(s, password="s3cret"):
    s.app_password = password


# ── auth.py token round-trip ────────────────────────────────────────────────

def test_session_token_roundtrip(settings_env):
    _enable_auth(settings_env)
    from app.auth import issue_session, verify_session

    tok = issue_session()
    assert verify_session(tok) is True
    assert verify_session(None) is False
    assert verify_session("garbage") is False
    assert verify_session(tok + "x") is False  # tampered signature


def test_session_token_expires(settings_env, monkeypatch):
    _enable_auth(settings_env)
    monkeypatch.setattr(settings_env, "app_session_ttl_seconds", 1)
    from app.auth import issue_session, verify_session

    tok = issue_session()
    assert verify_session(tok) is True
    real_now = time.time()
    monkeypatch.setattr("app.auth.time.time", lambda: real_now + 10)  # past the 1s TTL
    assert verify_session(tok) is False


def test_password_matches(settings_env):
    _enable_auth(settings_env, "hunter2")
    from app.auth import password_matches

    assert password_matches("hunter2") is True
    assert password_matches("wrong") is False


# ── enforcement middleware ──────────────────────────────────────────────────

def test_disabled_is_passthrough(settings_env):
    # app_password unset → no gate; status reports open.
    settings_env.app_password = ""
    client = TestClient(main.app)
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body == {"auth_enabled": False, "authenticated": True}
    # A normal protected route is reachable.
    assert client.get("/api/providers").status_code == 200


def test_protected_route_401_without_session(settings_env):
    _enable_auth(settings_env)
    client = TestClient(main.app)
    assert client.get("/api/providers").status_code == 401
    # status endpoint stays public (login gate needs it pre-session).
    s = client.get("/api/auth/status")
    assert s.status_code == 200
    assert s.json() == {"auth_enabled": True, "authenticated": False}
    # /health is the container liveness probe — must answer 200 without a session
    # or the docker healthcheck fails and the frontend never starts.
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"


def test_login_logout_flow(settings_env):
    _enable_auth(settings_env)
    client = TestClient(main.app)

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": "s3cret"})
    assert r.status_code == 200
    assert r.json()["authenticated"] is True
    # Cookie now persisted on the client → protected route opens.
    assert client.get("/api/providers").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True

    client.post("/api/auth/logout")
    assert client.get("/api/providers").status_code == 401


def test_login_cookie_is_persistent(settings_env):
    # The session cookie must carry an absolute Expires, not just Max-Age: Safari
    # ignores Max-Age and would otherwise treat it as a session cookie and drop it.
    _enable_auth(settings_env)
    client = TestClient(main.app)
    r = client.post("/api/auth/login", json={"password": "s3cret"})
    set_cookie = r.headers["set-cookie"]
    assert "bk_session=" in set_cookie
    assert "expires=" in set_cookie.lower()
    assert "max-age=" in set_cookie.lower()


def test_login_cookie_secure_opt_in(settings_env):
    # COOKIE_SECURE flips the Secure attribute (off by default for http LAN hosts).
    _enable_auth(settings_env)
    settings_env.cookie_secure = False
    plain = TestClient(main.app).post(
        "/api/auth/login", json={"password": "s3cret"}
    ).headers["set-cookie"]
    assert "secure" not in plain.lower()

    settings_env.cookie_secure = True
    secured = TestClient(main.app).post(
        "/api/auth/login", json={"password": "s3cret"}
    ).headers["set-cookie"]
    assert "secure" in secured.lower()


def test_ws_rejected_without_session(settings_env):
    _enable_auth(settings_env)
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


# ── web-console token fold-in ───────────────────────────────────────────────

def test_console_requires_token_property(settings_env):
    s = settings_env
    # app-auth on → token folded in (session is the gate).
    s.app_password = "s3cret"
    s.web_console_token = "legacy"
    assert s.console_requires_token is False
    # app-auth off but token set → legacy token still required.
    s.app_password = ""
    assert s.console_requires_token is True


def test_console_action_rides_session_when_app_auth_on(settings_env):
    s = settings_env
    _enable_auth(s)
    s.enable_web_console = True
    s.web_console_token = ""  # no legacy token at all
    assert s.web_console_available is True

    client = TestClient(main.app)
    # Unauthenticated → blocked by middleware.
    assert client.post("/api/providers/unknown/model", json={"model": "x"}).status_code == 401
    # Authenticated → passes the console gate; fails only on the unknown instance.
    client.post("/api/auth/login", json={"password": "s3cret"})
    r = client.post("/api/providers/unknown/model", json={"model": "x"})
    assert r.status_code == 404  # past auth + console gate, unknown instance
