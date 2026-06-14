"""
test_web_search.py — SearXNG-first web search with DDG fallback (P-0046 slice 5).

Locks the backend selection (SearXNG when configured, DDG otherwise), graceful
fallback when SearXNG errors, and the DDG parser fix (independent title/snippet
matching so results with no adjacent snippet are no longer silently dropped).
"""
from __future__ import annotations

from app.providers.tools import web_search


async def test_uses_searxng_when_configured(monkeypatch):
    monkeypatch.setattr(web_search._settings, "searxng_url", "http://searxng:8080")

    async def fake_searxng(query, n):
        return [{"url": "https://a.test", "title": "A", "snippet": "from searxng"}]

    async def fail_ddg(query, n):  # must NOT be called
        raise AssertionError("DDG should not run when SearXNG returns results")

    monkeypatch.setattr(web_search, "_search_searxng", fake_searxng)
    monkeypatch.setattr(web_search, "_search_ddg", fail_ddg)
    out = await web_search.run("hello")
    assert "from searxng" in out and "https://a.test" in out


async def test_falls_back_to_ddg_on_searxng_error(monkeypatch):
    monkeypatch.setattr(web_search._settings, "searxng_url", "http://searxng:8080")

    async def boom(query, n):
        raise RuntimeError("searxng down")

    async def fake_ddg(query, n):
        return [{"url": "https://b.test", "title": "B", "snippet": "from ddg"}]

    monkeypatch.setattr(web_search, "_search_searxng", boom)
    monkeypatch.setattr(web_search, "_search_ddg", fake_ddg)
    out = await web_search.run("hello")
    assert "from ddg" in out


async def test_skips_searxng_when_unset(monkeypatch):
    monkeypatch.setattr(web_search._settings, "searxng_url", "")

    async def fake_ddg(query, n):
        return [{"url": "https://c.test", "title": "C", "snippet": "ddg only"}]

    monkeypatch.setattr(web_search, "_search_ddg", fake_ddg)
    # _search_searxng must not be invoked (no url) — patch it to blow up if it is.
    async def guard(query, n):
        raise AssertionError("SearXNG must be skipped when searxng_url is empty")

    monkeypatch.setattr(web_search, "_search_searxng", guard)
    out = await web_search.run("hello")
    assert "ddg only" in out


def test_ddg_parser_keeps_results_without_adjacent_snippet():
    # Two results; only the first has a snippet. The old combined regex dropped the
    # second entirely; the independent matchers keep both (snippet empty for #2).
    html = (
        '<a class="result__a" href="https://1.test">First</a>'
        '<a class="result__snippet">snippet one</a>'
        '<a class="result__a" href="https://2.test">Second</a>'
    )
    rows = web_search._parse_ddg_html(html, 5)
    assert [r["url"] for r in rows] == ["https://1.test", "https://2.test"]
    assert rows[0]["snippet"] == "snippet one"
    assert rows[1]["snippet"] == ""
