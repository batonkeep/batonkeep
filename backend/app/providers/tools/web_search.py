"""
providers/tools/web_search.py — web search tool for the model executor agent loop.

Two backends, transparently (P-0046 slice 5):
  • **SearXNG (preferred)** — a self-hosted, key-free, multi-engine metasearch JSON
    API (aggregates Google/Brave/DDG/…). Better recall + source quality + robustness
    than scraping one engine's HTML (benchmarked 2026-06-13: ~10–22 vs ~5 results/
    query, authoritative domains over SEO farms). Enabled when `searxng_url` is set
    (Compose points it at the internal `searxng` service).
  • **DuckDuckGo HTML scrape (fallback)** — key-free, no dependency, but brittle (a
    markup change → silent-zero). Used automatically whenever SearXNG is unset, down,
    or returns nothing — so search always degrades rather than failing.

Both stay built-ins (not an MCP Tier-A server): same posture as the provider CLIs'
own built-in WebSearch — wrapping a scraper in MCP buys nothing.
"""
from __future__ import annotations

import logging
import re

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

TOOL_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for current information. Returns titles, snippets, and URLs.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "num_results": {
                "type": "integer",
                "default": 5,
                "description": "Max results to return.",
            },
        },
        "required": ["query"],
    },
}


async def run(query: str, num_results: int = 5) -> str:
    """Execute a web search and return formatted results.

    Tries SearXNG first (when configured); on any failure or empty result falls back
    to the DuckDuckGo HTML scrape so search degrades gracefully rather than failing.
    """
    results: list[dict] = []
    if _settings.searxng_url:
        try:
            results = await _search_searxng(query, num_results)
        except Exception as exc:  # noqa: BLE001 — fall back to DDG, never hard-fail
            logger.warning("[web_search] SearXNG failed (%s) — falling back to DDG", exc)

    if not results:
        try:
            results = await _search_ddg(query, num_results)
        except Exception as exc:  # noqa: BLE001
            return f"[web_search error] {exc}"

    if not results:
        return f"No results found for: {query}"
    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**\n   {r['snippet']}\n   URL: {r['url']}\n")
    return "\n".join(lines)


async def _search_searxng(query: str, num_results: int) -> list[dict]:
    """Query the self-hosted SearXNG JSON API."""
    url = _settings.searxng_url.rstrip("/") + "/search"
    async with httpx.AsyncClient(timeout=_settings.searxng_timeout_seconds) as client:
        resp = await client.get(
            url,
            params={"q": query, "format": "json"},
            headers={"User-Agent": "batonkeep-agent/0.1"},
        )
        resp.raise_for_status()
        data = resp.json()
    out: list[dict] = []
    for r in data.get("results", [])[:num_results]:
        out.append({
            "url": r.get("url", ""),
            "title": (r.get("title") or "").strip(),
            "snippet": (r.get("content") or "").strip(),
        })
    return out


async def _search_ddg(query: str, num_results: int) -> list[dict]:
    """DuckDuckGo HTML scrape (key-free fallback)."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "batonkeep-agent/0.1"},
        )
        resp.raise_for_status()
        return _parse_ddg_html(resp.text, num_results)


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    """Lightweight HTML scraper for DuckDuckGo results.

    Title/href and snippet are matched independently then zipped — the old single
    combined regex required a snippet immediately after every title and silently
    dropped ~50% of results (benchmark 2026-06-13: 9–10 in the HTML, 4–5 extracted)
    when a result was laid out differently."""
    titles = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL
    )
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL
    )

    def clean(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

    results: list[dict] = []
    for i, (url, title) in enumerate(titles[:max_results]):
        snippet = clean(snippets[i]) if i < len(snippets) else ""
        results.append({"url": url, "title": clean(title), "snippet": snippet})
    return results
