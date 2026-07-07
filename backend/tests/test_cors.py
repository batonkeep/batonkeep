"""
tests/test_cors.py — CORS origin allowlist (CORS_ALLOW_ORIGINS).

The default "*" reflect-any behaviour (with credentials + PNA) is covered by
test_pna.py. This covers the pin-down knob: parsing, and that an explicit
allowlist reflects an allowed origin while refusing a foreign one — the surface
a self-hoster closes when they don't use a public control plane.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import Settings


def test_cors_allow_origins_list_parses_and_trims():
    assert Settings(cors_allow_origins="*").cors_allow_origins_list == ["*"]
    assert Settings(
        cors_allow_origins="https://a.example.com, https://b.example.com ,"
    ).cors_allow_origins_list == ["https://a.example.com", "https://b.example.com"]


def _app(origins: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    return app


def test_pinned_allowlist_reflects_allowed_origin():
    allowed = "https://ui.example.com"
    client = TestClient(_app([allowed]))
    r = client.options(
        "/api/ping",
        headers={"Origin": allowed, "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") == allowed
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_pinned_allowlist_refuses_foreign_origin():
    client = TestClient(_app(["https://ui.example.com"]))
    r = client.options(
        "/api/ping",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # A non-allowed origin is never reflected, so the browser blocks the read.
    assert r.headers.get("access-control-allow-origin") is None
