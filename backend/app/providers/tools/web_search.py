"""
providers/tools/web_search.py — web search tool for the model executor agent loop.

Uses httpx to query the DuckDuckGo Instant Answer API (no key required).
Returns a structured result the model can read.
"""
from __future__ import annotations

import httpx

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
    """Execute a web search and return formatted results."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # DuckDuckGo HTML search (no API key needed)
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "batonkeep-agent/0.1"},
            )
            resp.raise_for_status()
            # Parse raw text for result snippets (lightweight, no lxml needed)
            text = resp.text
            results = _parse_ddg_html(text, num_results)
            if not results:
                return f"No results found for: {query}"
            lines = [f"Search results for: {query}\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. **{r['title']}**\n   {r['snippet']}\n   URL: {r['url']}\n")
            return "\n".join(lines)
    except Exception as exc:
        return f"[web_search error] {exc}"


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    """Very lightweight HTML scraper for DuckDuckGo results."""
    import re
    results = []
    # Find result blocks
    blocks = re.findall(
        r'class="result__title".*?href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</span>',
        html,
        re.DOTALL,
    )
    for url, title, snippet in blocks[:max_results]:
        def clean(s):
            return re.sub(r"<[^>]+>", "", s).strip()
        results.append({"url": url, "title": clean(title), "snippet": clean(snippet)})
    return results
