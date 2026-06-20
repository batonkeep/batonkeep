"""
tests/test_version.py — Version display + update check (D-0053).

Covers the running-version stamp surfaced on /health and /api/version, the
semver-comparison logic behind the "update available" hint, and the best-effort
latest-release lookup (cached, degrades to None when disabled or failing).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import version as appversion

# ── update_available semantics ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("0.4.0", "v0.4.1", True),
        ("v0.4.0", "v0.4.0", False),   # equal
        ("0.5.0", "v0.4.1", False),    # ahead of latest
        ("0.4.0", "v0.10.0", True),    # numeric, not lexical (10 > 4)
        ("dev", "v0.4.1", False),      # non-release current never nags
        ("0.4.0", None, False),        # no latest known
        ("0.4.0", "", False),
    ],
)
def test_update_available(current, latest, expected):
    assert appversion.update_available(current, latest) is expected


# ── latest_release: disabled + cache ────────────────────────────────────────

@pytest.mark.asyncio
async def test_latest_release_disabled_returns_none():
    assert await appversion.latest_release("", 3600) == (None, None)


@pytest.mark.asyncio
async def test_latest_release_failure_is_not_cached(monkeypatch):
    """A failed lookup must not poison the cache for the full TTL — the next
    call retries (covers the endpoint-not-yet-deployed case)."""
    appversion._cache = None
    calls = {"n": 0}

    class _OkResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"latest": "v1.2.3", "release_url": "https://example/r"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("endpoint 404 / network down")
            return _OkResp()

    monkeypatch.setattr(appversion.httpx, "AsyncClient", _Client)
    assert await appversion.latest_release("https://x/latest.json", 3600) == (None, None)
    # Not cached as None → second call actually retries and now succeeds.
    assert await appversion.latest_release("https://x/latest.json", 3600) == (
        "v1.2.3",
        "https://example/r",
    )
    assert calls["n"] == 2
    appversion._cache = None


@pytest.mark.asyncio
async def test_latest_release_caches(monkeypatch):
    appversion._cache = None
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):  # noqa: D401
            pass

        def json(self):
            return {"latest": "v9.9.9", "release_url": "https://example/r"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(appversion.httpx, "AsyncClient", _Client)
    first = await appversion.latest_release("https://x/latest.json", 3600)
    second = await appversion.latest_release("https://x/latest.json", 3600)
    assert first == ("v9.9.9", "https://example/r")
    assert second == first
    assert calls["n"] == 1  # second served from cache
    appversion._cache = None


# ── endpoints ───────────────────────────────────────────────────────────────

def test_health_reports_version():
    body = TestClient(main.app).get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == appversion.APP_VERSION


def test_version_endpoint_disabled_check(monkeypatch):
    # Patch the settings object main actually reads (it binds `settings` at import;
    # an earlier cache_clear can make get_settings() a different instance). Also
    # neutralise the check so the assertion never depends on live network.
    monkeypatch.setattr(main.settings, "version_check_url", "", raising=False)
    monkeypatch.setattr(appversion, "_cache", None, raising=False)
    body = TestClient(main.app).get("/api/version").json()
    assert body["version"] == appversion.APP_VERSION
    assert body["latest"] is None
    assert body["update_available"] is False
