"""Running-version provenance + best-effort latest-release lookup (D-0053).

The version is stamped into the image at build time from the `v*` git tag
(`BATONKEEP_VERSION` build arg → env, set by the release CI). Local/dev and
untagged builds fall back to a clear `dev` sentinel rather than a blank.

`latest_release()` reads a static `latest-version.json` served by our own public
site (`VERSION_CHECK_URL`, default `https://batonkeep.com/latest-version.json`),
*not* the GitHub API — no anonymous rate limits, no third-party phone-home, and
no instance data is sent. It is best-effort and cached: a slow/down/disabled
check never blocks the version endpoint, it just omits the "update available"
hint. Set `VERSION_CHECK_URL=""` to disable the check entirely.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

# Stamped at build time (see Dockerfile ARG/ENV); `dev` for local/untagged builds.
APP_VERSION: str = (os.getenv("BATONKEEP_VERSION") or "").strip() or "dev"


def _normalize(tag: str) -> str:
    """Compare on the bare numeric version (`v0.4.0` ~ `0.4.0`)."""
    return tag.strip().lstrip("vV")


def _is_release(version: str) -> bool:
    """Only real, comparable release stamps participate in the update hint."""
    head = _normalize(version).split("+", 1)[0].split("-", 1)[0]
    parts = head.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts)


def _version_tuple(version: str) -> tuple[int, ...]:
    head = _normalize(version).split("+", 1)[0].split("-", 1)[0]
    return tuple(int(p) for p in head.split(".") if p.isdigit())


def update_available(current: str, latest: str | None) -> bool:
    """True only when both are real releases and latest strictly exceeds current."""
    if not latest or not _is_release(current) or not _is_release(latest):
        return False
    return _version_tuple(latest) > _version_tuple(current)


@dataclass
class _Cached:
    at: float
    latest: str | None
    release_url: str | None


_cache: _Cached | None = None


async def latest_release(
    url: str, ttl_seconds: int, *, force: bool = False
) -> tuple[str | None, str | None]:
    """Best-effort `(latest_tag, release_url)` from the public CDN JSON.

    Cached for `ttl_seconds`; any failure (disabled, network, malformed) returns
    `(None, None)` so the caller degrades to a version-only response.
    """
    global _cache
    if not url:
        return None, None
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache.at) < ttl_seconds:
        return _cache.latest, _cache.release_url
    latest: str | None = None
    release_url: str | None = None
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, dict):
            latest = (data.get("latest") or "").strip() or None
            release_url = (data.get("release_url") or "").strip() or None
    except Exception:
        # Stale cache is better than nothing on a transient failure.
        if _cache is not None:
            return _cache.latest, _cache.release_url
        latest, release_url = None, None
    _cache = _Cached(at=now, latest=latest, release_url=release_url)
    return latest, release_url
