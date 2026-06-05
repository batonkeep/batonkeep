"""
providers/tools/_ssrf.py — SSRF guard for agent-driven outbound HTTP.

Agent tools fetch URLs the model (or a redirect, or a scraped page) chose. The
executor runs inside the backend container on the shared docker network, so an
unguarded fetch can reach internal services (postgres/redis/mongo), loopback, or
the cloud-metadata endpoint (169.254.169.254). This module rejects any URL whose
host resolves into a non-public address range, and provides a redirect-safe GET
that re-validates every hop (a public URL can 30x into an internal one).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

# Max redirect hops we follow while re-validating each one.
_MAX_REDIRECTS = 5


class SSRFError(Exception):
    """Raised when a URL targets a disallowed (non-public) address."""


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True for any address that isn't a normal, routable public host."""
    # is_global is the broad allow-test; the explicit checks cover ranges some
    # platforms classify inconsistently (and document intent).
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # includes 169.254.0.0/16 (cloud metadata)
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def assert_url_allowed(url: str) -> None:
    """
    Raise SSRFError unless `url` is http(s) to a host that resolves entirely to
    public IPs. Resolves DNS and checks every returned address (defends against
    a hostname with both a public and an internal A record).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFError(f"scheme not allowed: {scheme or '(none)'}")

    host = parts.hostname
    if not host:
        raise SSRFError("missing host")

    # A literal IP is checked directly; a name is resolved to all its addresses.
    try:
        literal = ipaddress.ip_address(host)
        addrs = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80))
        except socket.gaierror as exc:
            raise SSRFError(f"cannot resolve host: {host} ({exc})") from exc
        addrs = []
        for info in infos:
            sockaddr = info[4]
            try:
                addrs.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
        if not addrs:
            raise SSRFError(f"no addresses for host: {host}")

    for ip in addrs:
        if _ip_is_blocked(ip):
            raise SSRFError(f"host resolves to a non-public address: {host} -> {ip}")


async def safe_get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """
    GET that validates the target before each request and re-validates on every
    redirect hop. The client MUST be created with follow_redirects=False so this
    guard sees each Location instead of httpx silently following it.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        assert_url_allowed(current)
        resp = await client.get(current, **kwargs)
        if resp.is_redirect and "location" in resp.headers:
            current = str(resp.next_request.url) if resp.next_request else resp.headers["location"]
            continue
        return resp
    raise SSRFError(f"too many redirects (>{_MAX_REDIRECTS})")
