"""
providers/tools/web_fetch.py — fetch and extract text from a URL.
"""
from __future__ import annotations

import re

import httpx

from app.providers.tools._ssrf import SSRFError, safe_get

TOOL_SCHEMA = {
    "name": "web_fetch",
    "description": "Fetch and extract the main text content from a URL.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch."},
            "max_chars": {"type": "integer", "default": 8000, "description": "Truncate to this many characters."},
        },
        "required": ["url"],
    },
}


async def run(url: str, max_chars: int = 8000) -> str:
    try:
        # follow_redirects=False so the SSRF guard re-validates each hop itself
        # (a public URL can 30x into an internal address).
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            resp = await safe_get(
                client,
                url,
                headers={"User-Agent": "batonkeep-agent/0.1"},
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                text = _extract_text(resp.text)
            else:
                text = resp.text
            return text[:max_chars] + ("…" if len(text) > max_chars else "")
    except SSRFError as exc:
        return f"[web_fetch blocked] refusing to fetch a non-public address: {exc}"
    except Exception as exc:
        return f"[web_fetch error] {exc}"


def _extract_text(html: str) -> str:
    """Strip tags, collapse whitespace."""
    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text
