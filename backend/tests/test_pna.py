"""
tests/test_pna.py — Private Network Access preflight.

A page on a public origin (a hosted control plane) connecting to this backend on
a loopback/LAN address triggers Chrome's PNA preflight. Chrome blocks it unless
the response echoes ``Access-Control-Allow-Private-Network: true``. Verify the
middleware adds it for the WS + API endpoints, and only when the client asks.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

PUBLIC_ORIGIN = "https://grand-medovik-50a943.netlify.app"


def _preflight(path: str, pna: bool):
    headers = {"Origin": PUBLIC_ORIGIN, "Access-Control-Request-Method": "GET"}
    if pna:
        headers["Access-Control-Request-Private-Network"] = "true"
    return TestClient(app).options(path, headers=headers)


def test_pna_header_added_for_ws_and_api():
    for path in ("/ws", "/api/providers"):
        r = _preflight(path, pna=True)
        assert r.status_code == 200, path
        assert r.headers.get("access-control-allow-private-network") == "true", path
        # CORS still reflects the requesting origin.
        assert r.headers.get("access-control-allow-origin") == PUBLIC_ORIGIN, path


def test_pna_header_absent_without_request_header():
    # A normal preflight (no PNA request) must not advertise private-network access.
    r = _preflight("/api/providers", pna=False)
    assert r.headers.get("access-control-allow-private-network") is None
