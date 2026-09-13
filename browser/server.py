"""
browser/server.py — the Batonkeep browser sidecar ([[D-0073]] slices D1a + D1b).

**Why this exists as a separate image.** Slice D1a first shipped the browser *inside* the
backend image, which grew it from 3.41 GB to 5.49 GB — and every self-hoster paid that for
a capability that defaults to `off`. The browser is now its own optional image, started
only under the `browser` compose profile, so the base image carries **nothing**: not
Chromium, not its system libraries, not even the playwright python package (whose bundled
node driver is 126 MB on its own).

**Why a narrow HTTP endpoint rather than CDP or `playwright run-server`.** Both of those
expose *arbitrary browser control* to anything that can reach the port — navigate anywhere,
read any page, run any script, attach to any target. This exposes exactly one verb: give me
a URL, get back that page's visible text. If something else on the network reaches this
service, the difference between those two designs is the difference between a foothold and
a page fetch.

**Where the SSRF policy lives — deliberately not here.** The caller passes the proxy it
wants the browser to use, and the backend passes its own `ssrf_proxy`, which applies
`assert_url_allowed` to **every** hop. Duplicating that policy here would mean two
implementations of the rule that keeps an agent out of the cloud metadata endpoint, and
the second copy would be the one nobody updates. The sidecar stays a dumb renderer; the
fence stays in one place.

Consequently **this service must not be reachable by anything but the backend** — see the
`browsernet` network in `docker-compose.yml`, which is what actually enforces that.

**D1b keeps the one-verb property**, which is the point of the slice being shaped this way.
The endpoint is still "give me a URL, get that page's visible text" — now optionally after
following **one same-origin link**, resolved from an `<a href>` and navigated to as a plain
GET. It does not click, type, or submit, so reaching this port still buys a page fetch
rather than browser control. Nothing is held between requests: a whole browser per call,
discarded, exactly as in D1a.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("browser-sidecar")

app = FastAPI(title="batonkeep-browser", docs_url=None, redoc_url=None)

VERSION = os.environ.get("BATONKEEP_VERSION", "dev")

_LAUNCH_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",   # /dev/shm is small in containers; use /tmp instead
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-sync",
    "--no-first-run",
]
# NOT among them: `--no-sandbox`. Chromium's own sandbox is a real boundary and we keep
# it; ours goes underneath rather than in place of it.


class RenderRequest(BaseModel):
    url: str
    timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    #: The forward proxy every request must go through. The caller owns the egress
    #: policy (see the module docstring); we only point the browser at it.
    proxy: str | None = None
    #: D1b ([[D-0073]] slice D1b, navigation-only): the visible text of a **same-origin
    #: link** on the rendered page to follow. Exactly one hop; `_pick_link` resolves the
    #: label and `_render` navigates to the resolved `href` rather than clicking anything
    #: (the comment there is the argument for why that distinction is the whole slice).
    follow: str | None = None
    max_links: int = Field(default=50, ge=0, le=200)


class RenderResult(BaseModel):
    final_url: str
    text: str
    #: Same-origin links found on the page, in document order: `{"text": …, "url": …}`.
    #: D1a returned visible text only, which meant the page's links were **invisible to
    #: the agent** — it could read "Next page" but had no way to name where that went.
    #: That, not clicking, is the real capability gap ([[P-0116]]).
    links: list[dict] = Field(default_factory=list)
    #: Set when `follow` was requested: the link that was actually followed.
    followed: dict | None = None


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": VERSION, "service": "batonkeep-browser"}


#: Extract same-origin anchors as (visible text, absolute href). Runs in the page, so
#: `a.href` is already resolved against the document's base URL by the browser itself —
#: no URL joining of our own to get subtly wrong.
_LINKS_JS = """
(max) => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.href, document.baseURI); } catch { continue; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') continue;
    if (u.origin !== location.origin) continue;          // same-origin only
    const text = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!text) continue;
    const href = u.href;
    const key = text + '\\u0000' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ text: text.slice(0, 120), url: href });
    if (out.length >= max) break;
  }
  return out;
}
"""


class FollowError(RuntimeError):
    """The requested link could not be followed — reported to the agent as data."""


def _pick_link(links: list[dict], wanted: str) -> dict:
    """Resolve the operator-visible label to exactly one same-origin link.

    Exact match first, then unique case-insensitive substring. An **ambiguous** label is
    refused rather than guessed: picking one of several "Next" links on the agent's behalf
    would make the thing that ran differ from the thing that was approved, which is the
    whole property this slice is built to preserve.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        raise FollowError("no link text given")
    exact = [x for x in links if x["text"] == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Identical label, identical destination is not ambiguous — dedupe handled that;
        # identical label to different places is.
        if len({x["url"] for x in exact}) == 1:
            return exact[0]
        raise FollowError(
            f"{len(exact)} same-origin links are labelled {wanted!r} and they lead to "
            "different pages — name it more precisely"
        )
    low = wanted.lower()
    part = [x for x in links if low in x["text"].lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise FollowError(
            f"no same-origin link labelled {wanted!r} on that page. Only links on the "
            "same site can be followed, and only links — not buttons or forms."
        )
    raise FollowError(
        f"{len(part)} same-origin links match {wanted!r} "
        f"({', '.join(repr(x['text']) for x in part[:4])}…) — name it more precisely"
    )


async def _render(req: RenderRequest) -> RenderResult:
    from playwright.async_api import async_playwright

    args = list(_LAUNCH_ARGS)
    if req.proxy:
        args += [
            f"--proxy-server={req.proxy}",
            # Load-bearing: Chromium does not send loopback or link-local requests
            # through a proxy by default, so without this a redirect to 169.254.169.254
            # (cloud metadata) goes straight out, bypassing the fence entirely. Found
            # live on Runtime B when the page loaded.
            "--proxy-bypass-list=<-loopback>",
        ]

    async with async_playwright() as pw:
        # A whole browser per request, not a pooled one. It costs about a second, and it
        # buys a genuinely clean process every time: no shared cache, no shared storage,
        # no state carried from whatever the previous caller opened. D1a has nothing to
        # persist, so there is nothing to trade that isolation for.
        browser = await pw.chromium.launch(headless=True, args=args)
        try:
            context = await browser.new_context(
                user_agent="batonkeep-agent/0.1",
                java_script_enabled=True,
                accept_downloads=False,
            )
            page = await context.new_page()
            try:
                await page.goto(req.url, timeout=req.timeout_ms,
                                wait_until="domcontentloaded")
                # Best-effort settle for client-rendered pages. `networkidle` never
                # arrives on a page that polls, so its timeout is not an error — read
                # what has rendered rather than failing a perfectly usable page.
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: S110 - a polling page is still readable
                    pass
                links = await page.evaluate(_LINKS_JS, req.max_links)
                followed = None
                if req.follow:
                    # **Resolve and navigate — do not click.** This is the mechanism that
                    # makes "navigation-only" an enforced property rather than a hope.
                    # Clicking runs the element's JavaScript, so a `click` can submit a
                    # form, fire an `onclick` that POSTs, or do anything else the page
                    # chose; "it was only a click" would be a promise we cannot keep.
                    # Navigating to an `<a href>` we already resolved is a plain GET to a
                    # known same-origin URL — checkable *before* it happens, which is what
                    # lets the approval name the destination, and idempotent, which is
                    # what lets the stateless replay design work at all ([[P-0116]]).
                    followed = _pick_link(links, req.follow)
                    await page.goto(followed["url"], timeout=req.timeout_ms,
                                    wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:  # noqa: S110 - a polling page is still readable
                        pass
                    links = await page.evaluate(_LINKS_JS, req.max_links)
                final_url = page.url
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            finally:
                await context.close()
        finally:
            await browser.close()
    return RenderResult(final_url=final_url, text=text or "", links=links,
                        followed=followed)


@app.post("/render")
async def render(req: RenderRequest) -> dict:
    """Render one page. Errors are returned as data, not as 5xx.

    The caller turns this into a tool result a model reads, and a browser failing on a
    page is an ordinary outcome ("that site timed out"), not a fault in this service. A
    500 would make the two indistinguishable in the backend's logs.
    """
    try:
        # Belt and braces over the browser's own timeout: a hung launch is not covered
        # by `page.goto`'s deadline, and this service must not accumulate stuck workers.
        result = await asyncio.wait_for(_render(req), timeout=req.timeout_ms / 1000 + 15)
    except TimeoutError:
        return {"error": f"{req.url} did not finish loading in time"}
    except FollowError as exc:
        # A named link that is missing or ambiguous is an ordinary outcome the agent
        # should read and retry differently, not a fault in this service.
        return {"error": str(exc)}
    except Exception as exc:
        logger.warning("render failed for %s: %s", req.url, exc)
        return {"error": f"could not open {req.url}: {exc}"}
    return result.model_dump()
