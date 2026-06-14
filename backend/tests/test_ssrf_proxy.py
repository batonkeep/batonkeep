"""
test_ssrf_proxy.py — the SSRF egress fence for the fetch MCP server (P-0046 slice 4
follow-up). Verifies the forward proxy refuses internal/link-local targets (the
metadata endpoint, loopback) over both the CONNECT (https) and absolute-form (http)
paths, and that the fetch provider wires `--proxy-url` to it once started.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest

from app.providers.tools import ssrf_proxy


@pytest.fixture
async def proxy_url():
    """A proxy server bound to the *current* test's event loop (the module singleton
    would stay bound to whichever loop started it first — pytest-asyncio gives each
    test a fresh loop)."""
    server = await asyncio.start_server(ssrf_proxy._handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _send(proxy_url: str, raw: bytes) -> bytes:
    parts = urlsplit(proxy_url)
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    writer.write(raw)
    await writer.drain()
    line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
    writer.close()
    return line


async def test_connect_to_metadata_endpoint_is_refused(proxy_url):
    line = await _send(proxy_url, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"403" in line


async def test_connect_to_loopback_is_refused(proxy_url):
    line = await _send(proxy_url, b"CONNECT 127.0.0.1:22 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"403" in line


async def test_plain_http_to_loopback_is_refused(proxy_url):
    line = await _send(proxy_url, b"GET http://127.0.0.1:8000/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert b"403" in line


async def test_malformed_request_is_rejected(proxy_url):
    line = await _send(proxy_url, b"garbage\r\n\r\n")
    assert b"400" in line


async def test_fetch_provider_passes_proxy_url_when_started():
    await ssrf_proxy.ensure_started()
    from app.providers.tools.registry import _build_fetch_provider

    args = _build_fetch_provider()._extra_args()
    assert args[0] == "--proxy-url"
    assert args[1] == ssrf_proxy.current_url()


def test_fetch_provider_omits_proxy_url_when_proxy_unstarted(monkeypatch):
    # If the proxy never started, no --proxy-url is appended (server runs direct —
    # the dev/test fallback; in the container the lifespan always starts it).
    monkeypatch.setattr(ssrf_proxy, "current_url", lambda: None)
    from app.providers.tools.registry import _build_fetch_provider

    assert _build_fetch_provider()._extra_args() == []
