"""
seed.py — Insert representative seed tasks (§14).

Idempotent: runs only when the tasks table is empty.
All seeds use candidates=["mock"] so they run with zero credentials.
"""
from __future__ import annotations

import logging
from app.db import AsyncSessionLocal
from app.models import Task

logger = logging.getLogger(__name__)

# Routing template shipped for mock — real-plan candidates listed as comments.
# Operators update DEFAULT_CANDIDATES or per-task routing after `make auth`.
_MOCK_ROUTING = {
    "strategy": "capability",
    "candidates": ["mock"],
    "capability_tags": [],
    "failover": True,
    "max_attempts": 1,
}


SEED_TASKS = [
    {
        "name": "Daily AI Ecosystem Brief",
        "description": "Latest AI model releases, coding agents/CLIs, launches, and notable papers.",
        "category": "research",
        "prompt_template": (
            "You are an autonomous research agent. Survey the most important AI/ML developments "
            "in the last 24–48 hours: model releases, coding-agent/CLI launches, notable papers, "
            "and significant product updates. Group findings by theme. For each item include a "
            "brief description and a source link. Produce a polished Markdown report with a # title, "
            "2–3 sentence executive summary, then organised sections."
        ),
        "params": {},
        "schedule_kind": "cron",
        "schedule_expr": "0 7 * * *",
        "want_markdown": True,
        "want_json": True,
        "routing": {
            # Suggested live order: claude › agy › grok (synthesis-strong)
            **_MOCK_ROUTING,
            "capability_tags": ["synthesis"],
        },
    },
    {
        "name": "Daily Macro Market Brief",
        "description": "Indices, rates/yields, FX majors, commodities, and daily economic events.",
        "category": "research",
        "prompt_template": (
            "You are a market research agent. Provide a concise but complete macro snapshot for "
            "today: major equity indices, 10Y bond yields (US/DE/JP), FX majors, key commodities "
            "(gold, crude, natural gas), and today's scheduled economic events + outcomes. "
            "Note significant moves and their likely drivers. Produce a Markdown report with a "
            "# title, brief summary, and organised sections. Include a ```json block with key "
            "figures as structured data."
        ),
        "params": {},
        "schedule_kind": "cron",
        "schedule_expr": "30 6 * * 1-5",
        "want_markdown": True,
        "want_json": True,
        "routing": {
            # Suggested live order: grok › claude (realtime/market-data-strong)
            **_MOCK_ROUTING,
            "capability_tags": ["realtime", "markets"],
        },
    },
    {
        "name": "Frontier LLM Comparison",
        "description": "Side-by-side comparison of the latest frontier models across all major labs.",
        "category": "comparison",
        "prompt_template": (
            "Compare the latest frontier LLMs from {labs}. For each: name/version, context window, "
            "key capabilities, notable strengths and weaknesses, and pricing. End with a recommendation "
            "table (model / best-for / price tier) and a 2–3 sentence verdict. Output a Markdown "
            "report and a structured ```json block with the comparison data."
        ),
        "params": {"labs": "OpenAI, Anthropic, Google, xAI, Meta"},
        "schedule_kind": "none",
        "schedule_expr": "",
        "want_markdown": True,
        "want_json": True,
        "routing": {
            # Fixed: always use a single best synthesis model
            "strategy": "fixed",
            "candidates": ["mock"],
            "capability_tags": ["synthesis"],
            "failover": False,
            "max_attempts": 1,
        },
    },
    {
        "name": "Flight Watch",
        "description": "Best-value flight search for flexible date windows; flag the single best pick.",
        "category": "action",
        "prompt_template": (
            "Search for the best-value flights from {origin} to {destination} around {date_window} "
            "for {pax} passenger(s) in {cabin} class. Rank by total cost, then travel time. "
            "Flag the single best value option with a clear recommendation. "
            "Output a Markdown comparison table and a ```json watchlist block with the top 5 options."
        ),
        "params": {
            "origin": "SYD",
            "destination": "LHR",
            "date_window": "2026-08-01 ± 3 days",
            "pax": "1",
            "cabin": "economy",
        },
        "schedule_kind": "interval",
        "schedule_expr": "21600",  # every 6 hours
        "want_markdown": True,
        "want_json": True,
        "routing": {
            # Suggested live order: agy › claude (long-context + flights tool)
            **_MOCK_ROUTING,
            "capability_tags": ["longcontext"],
        },
    },
]


async def seed_if_empty(owner_id: str = "local") -> None:
    """Insert seed tasks only if the tasks table is empty."""
    from sqlalchemy import select, func
    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count(Task.id)))
        if count and count > 0:
            logger.info("[seed] tasks table non-empty (%d rows) — skipping seed", count)
            return

        for seed in SEED_TASKS:
            task = Task(
                owner_id=owner_id,
                name=seed["name"],
                description=seed.get("description", ""),
                category=seed["category"],
                prompt_template=seed["prompt_template"],
                params=seed.get("params", {}),
                schedule_kind=seed["schedule_kind"],
                schedule_expr=seed["schedule_expr"],
                want_markdown=seed["want_markdown"],
                want_json=seed["want_json"],
                routing=seed["routing"],
                enabled=True,
            )
            db.add(task)

        await db.commit()
        logger.info("[seed] inserted %d seed tasks for owner=%s", len(SEED_TASKS), owner_id)


# Allow `python -m app.seed` (used by `make seed`) to seed against the configured DB.
if __name__ == "__main__":
    import asyncio
    import logging as _logging

    from app.config import get_settings
    from app.db import init_db

    _logging.basicConfig(level=_logging.INFO)

    async def _main() -> None:
        await init_db()
        # Ensure the owner row exists before inserting owned tasks.
        from app.models import Owner
        settings = get_settings()
        async with AsyncSessionLocal() as db:
            if await db.get(Owner, settings.owner_id) is None:
                db.add(Owner(id=settings.owner_id, label="Local operator"))
                await db.commit()
        await seed_if_empty(settings.owner_id)

    asyncio.run(_main())
