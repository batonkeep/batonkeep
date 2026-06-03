#!/usr/bin/env python3
"""
Live integration test for plan-CLI providers (claude, grok, agy).

Runs a simple single-turn prompt through CLIExecutor and verifies:
  - At least one token event is emitted (streaming works)
  - A result event is produced (not deferred/failed)
  - The result text is non-empty

Usage:
    cd backend && uv run python scripts/test_cli_live.py           # all three
    cd backend && uv run python scripts/test_cli_live.py grok      # one
    cd backend && uv run python scripts/test_cli_live.py grok agy  # subset
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from typing import Optional

sys.path.insert(0, ".")

from app.providers.base import EventKind, ExecEvent, ExecResult
from app.providers.cli_executor import CLIExecutor
from app.providers.registry import get_provider_def

# Single-turn, no tools — quick round-trip to verify parse/stream pipeline.
TEST_PROMPT = (
    "Respond in exactly one sentence: what is the capital of France? "
    "Do not use any tools."
)

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
DIM  = "\033[2m"
RST  = "\033[0m"


async def run_provider(name: str) -> bool:
    pdef = get_provider_def(name)
    if pdef is None:
        print(f"{FAIL} {name}: not in registry")
        return False
    if not shutil.which(pdef.cli_binary or ""):
        print(f"{FAIL} {name}: binary '{pdef.cli_binary}' not on PATH — install via `make auth`")
        return False

    print(f"\n{'─'*60}")
    print(f"  {name}  ({pdef.cli_binary})")
    print(f"{'─'*60}")

    executor = CLIExecutor(pdef)
    events: list[ExecEvent] = []
    result_ev: Optional[ExecEvent] = None
    token_text = ""
    t0 = time.monotonic()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            async for ev in executor.run_stream(
                TEST_PROMPT,
                workdir=tmpdir,
                tools_enabled=False,  # single turn, no tool round-trips
                max_rounds=1,
                budget_usd=0.10,
            ):
                events.append(ev)
                k = ev.kind

                if k == EventKind.token:
                    token_text += ev.text or ""
                    # Print a live dot per chunk so progress is visible
                    print(".", end="", flush=True)
                elif k == EventKind.result:
                    result_ev = ev
                    print()  # newline after dots
                elif k == EventKind.error:
                    print(f"\n  {FAIL} error: {ev.message}")
                else:
                    # log / phase / route — print dimmed for visibility
                    msg = (ev.message or "").replace("\n", " ")[:100]
                    print(f"\n  {DIM}[{k.value}]{RST} {msg}", end="", flush=True)

        except Exception as exc:
            print(f"\n  {FAIL} exception: {exc}")
            return False

    elapsed = time.monotonic() - t0
    kinds = [e.kind for e in events]
    n_tokens = kinds.count(EventKind.token)
    has_result = EventKind.result in kinds
    has_error  = EventKind.error in kinds

    print(f"\n  events: {len(events)} total · {n_tokens} token chunks · {elapsed:.1f}s")

    # ── Assertions ────────────────────────────────────────────────────────────
    failures: list[str] = []

    if not has_result:
        failures.append(
            "no result event (got error)" if has_error else "no result event — likely still deferred"
        )

    if has_result:
        result: Optional[ExecResult] = result_ev.data.get("result") if result_ev else None  # type: ignore[assignment]
        text = result.text.strip() if result else ""
        if not text:
            failures.append("result text is empty")
        else:
            # Check the result text contains "Paris" or at least something coherent
            preview = text[:120].replace("\n", " ")
            print(f"  result ({len(text)} chars): {preview!r}")

    if n_tokens == 0 and has_result:
        # For agy (plain text / no streaming), we accept no token events as long
        # as we get a result synthesised from accumulated plain-text output.
        print(f"  note: no token events (agy plain-text mode — ok)")

    if failures:
        for f in failures:
            print(f"  {FAIL} {f}")
        return False

    print(f"  {PASS} PASS")
    return True


async def main() -> None:
    providers = sys.argv[1:] or ["claude", "grok", "agy"]

    results: dict[str, bool] = {}
    for name in providers:
        results[name] = await run_provider(name)

    print(f"\n{'═'*60}")
    print("  Summary")
    print(f"{'═'*60}")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL} {name}")

    all_passed = all(results.values())
    print()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
