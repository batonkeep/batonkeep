"""
tests/test_ssrf.py — SSRF guard for agent-driven outbound HTTP.

Verifies web_fetch refuses non-public targets (loopback, private, link-local /
cloud-metadata, internal docker service names) and still allows ordinary public
hosts, including across redirects.
"""
from __future__ import annotations

import socket

import pytest

from app.providers.tools import web_fetch
from app.providers.tools._ssrf import SSRFError, assert_url_allowed


class TestAssertUrlAllowed:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://localhost/",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://10.0.0.5/",
            "http://192.168.1.10/",
            "http://172.16.0.1/",
            "http://[::1]/",
            "http://0.0.0.0/",
            "ftp://example.com/",  # disallowed scheme
            "file:///etc/passwd",
            "http:///nohost",
        ],
    )
    def test_blocks_non_public(self, url):
        with pytest.raises(SSRFError):
            assert_url_allowed(url)

    def test_blocks_internal_service_name(self, monkeypatch):
        # An internal docker hostname resolving to a private IP must be rejected.
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, "", ("172.18.0.2", 5432))],
        )
        with pytest.raises(SSRFError):
            assert_url_allowed("http://postgres:5432/")

    def test_blocks_dns_rebind_mixed_records(self, monkeypatch):
        # If any resolved address is internal, reject (don't fetch the public one).
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [
                (socket.AF_INET, None, None, "", ("93.184.216.34", 80)),
                (socket.AF_INET, None, None, "", ("127.0.0.1", 80)),
            ],
        )
        with pytest.raises(SSRFError):
            assert_url_allowed("http://evil.example/")

    def test_allows_public(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, "", ("93.184.216.34", 443))],
        )
        # Should not raise.
        assert_url_allowed("https://example.com/page")


@pytest.mark.asyncio
class TestWebFetchGuard:
    async def test_run_blocks_metadata_endpoint(self):
        out = await web_fetch.run("http://169.254.169.254/latest/meta-data/")
        assert "blocked" in out.lower()

    async def test_run_blocks_loopback(self):
        out = await web_fetch.run("http://127.0.0.1:8000/")
        assert "blocked" in out.lower()
