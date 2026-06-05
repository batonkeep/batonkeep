"""
tests/test_p9_sensitivity.py — P-0009 #1: sensitivity-aware routing.

The sovereignty boundary: a task marked `sensitivity: confidential` may only
ever resolve to a *local* provider, and must fail closed (defer) rather than
fall back to any remote API/CLI.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.providers.registry import (
    get_provider_def,
    local_candidate_ids,
)
from app.quota import QuotaTracker
from app.router import CandidatePlan, DeferredResult, resolve


def fresh_quota() -> QuotaTracker:
    return QuotaTracker()


def _confidential(candidates=None, tags=None, overflow_to=None) -> dict:
    return {
        "strategy": "capability",
        "sensitivity": "confidential",
        "candidates": candidates or ["mock"],
        "capability_tags": tags or [],
        "overflow_to": overflow_to,
    }


# ── Registry wiring ────────────────────────────────────────────────────────────

class TestLocalProviderWiring:
    def test_ollama_is_registered_and_local(self):
        pdef = get_provider_def("ollama")
        assert pdef is not None
        assert pdef.local is True
        assert pdef.tier == "open"
        assert "local" in pdef.capability_tags

    def test_remote_providers_are_not_local(self):
        for name in ("claude-api", "openai-api", "grok-api", "gemini-api", "open-default", "mock"):
            pdef = get_provider_def(name)
            assert pdef is not None and pdef.local is False, name

    def test_local_candidate_ids_lists_only_local(self):
        ids = local_candidate_ids()
        assert "ollama" in ids
        assert "claude-api" not in ids
        assert "mock" not in ids


# ── Confidential routing policy ────────────────────────────────────────────────

class TestConfidentialRouting:
    def test_routes_only_to_local_ignoring_declared_remotes(self):
        """Even if the task declares remote candidates, confidential work goes local."""
        q = fresh_quota()
        result = resolve(_confidential(candidates=["claude-api", "openai-api"]), q)
        assert isinstance(result, CandidatePlan)
        assert result.candidates == ["ollama"]

    def test_overflow_is_forbidden_offbox(self):
        q = fresh_quota()
        result = resolve(_confidential(candidates=["mock"], overflow_to="claude-api"), q)
        assert isinstance(result, CandidatePlan)
        assert result.overflow_to is None

    def test_fails_closed_when_local_is_cooling(self):
        """No healthy local provider → defer, never fall back to a remote."""
        q = fresh_quota()
        q.mark_cooldown("ollama", reset_at=datetime.now(UTC) + timedelta(minutes=30))
        result = resolve(_confidential(), q)
        assert isinstance(result, DeferredResult)
        assert "ollama" in result.cooling_providers

    def test_non_confidential_does_not_pull_in_local(self):
        """A normal task is unaffected — ollama isn't silently added."""
        q = fresh_quota()
        result = resolve({"strategy": "capability", "candidates": ["mock"]}, q)
        assert isinstance(result, CandidatePlan)
        assert result.candidates == ["mock"]
        assert "ollama" not in result.candidates
