"""
browser/server.py — the Batonkeep browser sidecar ([[D-0073]] slice D1a).

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


class RenderResult(BaseModel):
    final_url: str
    text: str


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": VERSION, "service": "batonkeep-browser"}


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
                final_url = page.url
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            finally:
                await context.close()
        finally:
            await browser.close()
    return RenderResult(final_url=final_url, text=text or "")


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
    except Exception as exc:
        logger.warning("render failed for %s: %s", req.url, exc)
        return {"error": f"could not open {req.url}: {exc}"}
    return result.model_dump()
