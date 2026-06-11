"""
tasks/templates.py — starter task presets the UI offers on a fresh install.

A new install's task list is empty (the "No tasks yet" state). These presets give
the user a one-click way to populate it with a real, useful scheduled task instead
of starting from a blank form. They mirror the session-task-type pattern in
`sessions/templates.py`: code-defined, served read-only, and applied by pre-filling
the task form — they are **not** auto-inserted into the database.

Each preset is seeded **disabled** (`enabled=False`): the schedule and provider route
are the user's to review before the task starts firing on its own. Prompts use
`{placeholder}` tokens (the same convention the task form's AI prompt-builder emits)
and stay provider-neutral and outcome-focused so they fail over cleanly across the
provider chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TaskTemplate:
    id: str
    label: str
    description: str
    # A TaskCreate-shaped payload used to pre-fill the task form. Seeded enabled=False.
    input: dict[str, Any]


# Ordered for display.
_TEMPLATES: list[TaskTemplate] = [
    TaskTemplate(
        id="ai-ecosystem-brief",
        label="Daily AI Ecosystem Brief",
        description=(
            "A scheduled morning roundup of the most important AI ecosystem "
            "developments — model releases, research, funding, policy."
        ),
        input={
            "name": "Daily AI Ecosystem Brief",
            "description": "Daily roundup of notable AI ecosystem developments.",
            "category": "research",
            "prompt_template": (
                "Research and summarise the most important developments in the AI "
                "ecosystem from the last {timeframe}.\n\n"
                "Cover: notable model releases and updates, significant research, major "
                "funding/company moves, and relevant policy or regulation. Prioritise what "
                "is genuinely important and recent; verify claims against reputable primary "
                "or secondary sources and link them.\n\n"
                "Produce a Markdown report: a `#` title with today's date, a 2–3 sentence "
                "executive summary, then organised sections with inline source links. Be "
                "concise and specific — cite every non-obvious claim. If nothing material "
                "happened in a category, say so rather than padding."
            ),
            "params": {"timeframe": "24 hours"},
            "schedule_kind": "cron",
            "schedule_expr": "0 8 * * *",  # 08:00 daily, in the task's timezone
            "timezone": "UTC",
            "want_markdown": True,
            "want_json": False,
            "enabled": False,
        },
    ),
    TaskTemplate(
        id="flight-watch",
        label="Flight Watch",
        description=(
            "Track fares for a route on a schedule and flag meaningful price "
            "movements — uses the built-in flight fare-lookup tool."
        ),
        input={
            "name": "Flight Watch",
            "description": "Monitor fares for a route and flag notable price drops.",
            "category": "data",
            "prompt_template": (
                "Check current flight fares from {origin} to {destination} for travel "
                "within {travel_window}, using the flight fare-lookup tool.\n\n"
                "Report the cheapest current fares with their dates and any notable "
                "options (non-stop vs. connecting, refundable). Call out fares that look "
                "like a meaningful drop or a particularly good deal for this route, and "
                "note anything a traveller should act on soon.\n\n"
                "Produce a short Markdown summary: the headline cheapest fare up top, then "
                "a compact table of the best options. State the lookup date so the prices "
                "are interpretable. If the lookup returns no data, say so plainly rather "
                "than inventing fares."
            ),
            "params": {
                "origin": "SFO",
                "destination": "JFK",
                "travel_window": "the next 3 months",
            },
            "schedule_kind": "cron",
            "schedule_expr": "0 7 * * *",  # 07:00 daily, in the task's timezone
            "timezone": "UTC",
            "want_markdown": True,
            "want_json": False,
            "enabled": False,
        },
    ),
]

_BY_ID = {t.id: t for t in _TEMPLATES}


def list_templates() -> list[TaskTemplate]:
    return list(_TEMPLATES)


def get_template(template_id: str) -> TaskTemplate | None:
    return _BY_ID.get(template_id)
