"""
providers/tools/flights.py — flight search tool stub.

Ships as a stub; wire a real provider (Google Flights API / Skyscanner) for production.
Returns mock fare data so the orchestrator + agent loop can be verified end-to-end.
"""
from __future__ import annotations

TOOL_SCHEMA = {
    "name": "flights",
    "description": (
        "Search for flights between two airports around a date window. "
        "Returns a comparison of available options ranked by value."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "IATA origin airport code (e.g. SYD)."},
            "destination": {
                "type": "string",
                "description": "IATA destination airport code (e.g. LHR).",
            },
            "date_window": {
                "type": "string",
                "description": "Date range, e.g. '2026-07-01 to 2026-07-07'.",
            },
            "pax": {"type": "integer", "default": 1},
            "cabin": {
                "type": "string",
                "default": "economy",
                "enum": ["economy", "business", "first"],
            },
        },
        "required": ["origin", "destination", "date_window"],
    },
}


async def run(
    origin: str,
    destination: str,
    date_window: str,
    pax: int = 1,
    cabin: str = "economy",
) -> str:
    """Stub implementation — returns mock fare data."""
    return (
        f"[STUB] Flight search: {origin} → {destination} | {date_window} | "
        f"{pax} pax | {cabin}\n\n"
        "| # | Date | Airline | Duration | Stops | Price (AUD) |\n"
        "|---|------|---------|----------|-------|-------------|\n"
        "| 1 | 2026-07-02 | Qantas | 22h 10m | 1 | $1,450 |\n"
        "| 2 | 2026-07-03 | Singapore Air | 21h 55m | 1 | $1,380 |\n"
        "| 3 | 2026-07-01 | Emirates | 23h 30m | 1 | $1,290 |\n\n"
        "**Best value:** Option 3 (Emirates, $1,290) — lowest price, slightly longer.\n\n"
        "*Note: stub data only. Wire a real flights API for production.*"
    )
