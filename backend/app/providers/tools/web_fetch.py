"""
providers/tools/web_fetch.py — fetch and extract text from a URL.
"""
from __future__ import annotations

import re

import httpx

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
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(
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
