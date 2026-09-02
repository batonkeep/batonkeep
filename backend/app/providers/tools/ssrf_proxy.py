"""
providers/tools/ssrf_proxy.py — SSRF egress fence for the `fetch` MCP server.

The curated `fetch` Tier-A server (`mcp-server-fetch`, P-0046 slice 4) is third-party
code that makes its own outbound HTTP and does NOT honour our `_ssrf` guard — so left
unfenced it could reach loopback / private / link-local addresses (e.g. the cloud
metadata endpoint 169.254.169.254) that the in-process `web_fetch` built-in blocks.

This module runs a tiny **local forward proxy** that applies exactly the same
`_ssrf.assert_url_allowed` allow/deny logic to every target, and we launch the fetch
server with `--proxy-url` pointed at it. The fetch server therefore inherits our SSRF
policy without us reimplementing or monkeypatching its HTTP stack.

Scope: it handles the two shapes httpx (the fetch server's client) uses through a
proxy — `CONNECT host:port` for https tunnelling and absolute-form requests for plain
http — validating the host before connecting and refusing disallowed targets with
`403`. Bound to 127.0.0.1 on an ephemeral port; started once for the app's lifetime.

Residual: validation re-resolves DNS at connect time rather than pinning the exact
validated IP, so a sub-second DNS-rebind between check and connect is not closed here
(low risk; the primary defence — refusing internal hosts — holds). Pin the IP if this
ever fronts untrusted multi-tenant traffic.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from app.config import get_settings
from app.providers.tools._ssrf import SSRFError, assert_url_allowed

logger = logging.getLogger(__name__)

_server: asyncio.AbstractServer | None = None
_url: str | None = None
#: (host, port) actually bound — `sidecar_url()` needs the port with a routable host.
_bind: tuple[str, int] | None = None


async def ensure_started() -> str:
    """Start the proxy once (idempotent) and return its URL.

    Loopback-only by default. It binds wider **only** when the browser sidecar is
    configured ([[D-0073]]), because that browser runs in a different container and
    `127.0.0.1` there is its own loopback, not ours — a fence it cannot reach is not a
    fence. The trade is stated rather than buried: on the compose network the proxy is
    then reachable by our other services, so a compromised sibling could use it to make
    outbound requests. It cannot use it to reach anything *internal* — refusing
    non-public addresses is the proxy's entire job — so what it gains is a relay to the
    public internet it could largely reach anyway, not a pivot inwards.
    """
    global _server, _url
    if _server is None:
        host = "0.0.0.0" if get_settings().browser_url else "127.0.0.1"  # noqa: S104
        server = await asyncio.start_server(_handle, host, 0)
        port = server.sockets[0].getsockname()[1]
        _server = server
        # The advertised URL stays loopback for in-process callers; `sidecar_url()` is
        # what a different container must use.
        _url = f"http://127.0.0.1:{port}"
        _bind = (host, port)
        globals()["_bind"] = _bind
        logger.info(
            "[ssrf-proxy] SSRF egress fence listening on %s:%d (advertised %s)",
            host, port, _url,
        )
    return _url  # type: ignore[return-value]


def sidecar_url(hostname: str) -> str | None:
    """The proxy URL as reachable from another container, or None if not started.

    `hostname` is how that container addresses *us* on the compose network — taken from
    `BROWSER_PROXY_HOST` so a non-compose deployment can correct it rather than being
    silently wrong.
    """
    bind = globals().get("_bind")
    if not bind:
        return None
    return f"http://{hostname}:{bind[1]}"


def current_url() -> str | None:
    """The running proxy URL, or None if it hasn't been started."""
    return _url


async def _validate(url: str) -> None:
    """Run the (DNS-resolving, blocking) SSRF check off the event loop."""
    await asyncio.to_thread(assert_url_allowed, url)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await reader.readuntil(b"\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        _close(writer)
        return
    parts = request_line.decode("latin-1").strip().split(" ")
    if len(parts) != 3:
        await _reject(writer, 400, "malformed request")
        return
    method, target, _version = parts

    # Collect the request headers (up to the blank line) — needed verbatim to forward
    # the plain-http case; harmless to read for CONNECT.
    headers = b""
    try:
        while True:
            line = await reader.readuntil(b"\r\n")
            headers += line
            if line in (b"\r\n", b"\n"):
                break
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        _close(writer)
        return

    if method.upper() == "CONNECT":
        await _handle_connect(target, reader, writer)
    else:
        await _handle_plain(method, target, headers, writer)


async def _handle_connect(
    target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    host, _, port_s = target.partition(":")
    try:
        port = int(port_s) if port_s else 443
    except ValueError:
        await _reject(writer, 400, "bad CONNECT target")
        return
    try:
        await _validate(f"https://{host}:{port}")
    except SSRFError as exc:
        logger.warning("[ssrf-proxy] refused CONNECT %s: %s", target, exc)
        await _reject(writer, 403, f"blocked by SSRF policy: {exc}")
        return
    try:
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        await _reject(writer, 502, f"upstream connect failed: {exc}")
        return
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(
        _pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True
    )
    _close(up_writer)
    _close(writer)


async def _handle_plain(
    method: str, target: str, headers: bytes, writer: asyncio.StreamWriter
) -> None:
    try:
        await _validate(target)
    except SSRFError as exc:
        logger.warning("[ssrf-proxy] refused %s %s: %s", method, target, exc)
        await _reject(writer, 403, f"blocked by SSRF policy: {exc}")
        return
    parts = urlsplit(target)
    host = parts.hostname or ""
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        await _reject(writer, 502, f"upstream connect failed: {exc}")
        return
    # Re-issue in origin form (path, not absolute URL) and replay the headers. The
    # fetch server only issues GET, so there is no request body to forward.
    up_writer.write(f"{method} {path} HTTP/1.1\r\n".encode("latin-1") + headers)
    await up_writer.drain()
    await _pipe(up_reader, writer)
    _close(up_writer)
    _close(writer)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass


async def _reject(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(code, "Error")
    body = msg.encode("utf-8", "replace")
    try:
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("latin-1") + body
        )
        await writer.drain()
    except (ConnectionError, RuntimeError):
        pass
    _close(writer)


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except (ConnectionError, RuntimeError):
        pass
