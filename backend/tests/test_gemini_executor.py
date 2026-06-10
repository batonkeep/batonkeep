"""
test_gemini_executor.py — native google-genai executor (D-0034 / P-0043).

The reason this path exists at all: the OpenAI-compat shim rebuilds an assistant
tool-call from only id/name/arguments and drops Gemini's `thought_signature`,
which thinking models require replayed on every later turn — so the *second* tool
round 400s ("Function call is missing a thought_signature"). The native path must
replay the model's `Content` parts verbatim so the signature survives. The core
test below asserts exactly that round-trip.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import types

from app.providers.base import EventKind
from app.providers.model_executor import ModelExecutor
from app.providers.registry import get_provider_def


class _FakeChunk:
    def __init__(self, parts, usage=None):
        content = SimpleNamespace(parts=parts)
        self.candidates = [SimpleNamespace(content=content)]
        self.usage_metadata = usage


def _usage(pin=10, pout=5, thoughts=3):
    return SimpleNamespace(
        prompt_token_count=pin,
        candidates_token_count=pout,
        thoughts_token_count=thoughts,
    )


class _FakeModels:
    """Replays a scripted list of per-round (parts, usage) tuples and snapshots
    the `contents` it is handed on each call so the round-trip can be asserted."""

    def __init__(self, rounds):
        self._rounds = rounds
        self.contents_snapshots: list[list] = []

    async def generate_content_stream(self, *, model, contents, config):
        self.contents_snapshots.append(list(contents))
        parts, usage = self._rounds[len(self.contents_snapshots) - 1]

        async def _gen():
            for p in parts:
                yield _FakeChunk([p])
            yield _FakeChunk([], usage)

        return _gen()


class _FakeClient:
    def __init__(self, models):
        self.aio = SimpleNamespace(models=models)


@pytest.fixture
def gemini_executor(monkeypatch):
    async def _fake_key(*a, **k):
        return "fake-key"

    monkeypatch.setattr("app.credentials.resolve_api_key", _fake_key)

    async def _fake_tool(self, name, args_json, *, workdir):
        return f"[tool {name} result]"

    monkeypatch.setattr(ModelExecutor, "_call_tool", _fake_tool)

    pdef = get_provider_def("gemini-api")
    assert pdef is not None and pdef.kind == "gemini"
    return ModelExecutor(pdef), monkeypatch


async def _drain(executor):
    return [ev async for ev in executor.run_stream("research X", workdir="/tmp")]


async def test_thought_signature_is_replayed_on_second_round(gemini_executor):
    executor, monkeypatch = gemini_executor

    sig = b"THOUGHT_SIG_ABC"
    fcall_part = types.Part(
        function_call=types.FunctionCall(name="web_search", args={"query": "X"}),
        thought_signature=sig,
    )
    final_part = types.Part(text="# Report\n\nFinal answer.")

    fake_models = _FakeModels([
        ([fcall_part], _usage()),       # round 1: a tool call carrying the signature
        ([final_part], _usage()),       # round 2: the final answer (no tool call)
    ])
    monkeypatch.setattr(
        "google.genai.Client", lambda *a, **k: _FakeClient(fake_models)
    )

    await _drain(executor)

    # The model turn was replayed on the second call WITH the thought_signature intact.
    assert len(fake_models.contents_snapshots) == 2
    second_contents = fake_models.contents_snapshots[1]
    model_turns = [c for c in second_contents if c.role == "model"]
    assert model_turns, "model turn was not replayed into history"
    replayed = [
        p for turn in model_turns for p in turn.parts
        if p.function_call and p.thought_signature == sig
    ]
    assert replayed, "thought_signature was dropped — the shim bug would recur"

    # The tool result was fed back as a function_response.
    fn_responses = [
        p for c in second_contents if c.role == "user"
        for p in c.parts if p.function_response
    ]
    assert any(p.function_response.name == "web_search" for p in fn_responses)


async def test_streams_tokens_tool_and_result(gemini_executor):
    executor, monkeypatch = gemini_executor

    fcall_part = types.Part(
        function_call=types.FunctionCall(name="web_search", args={"query": "X"}),
        thought_signature=b"S",
    )
    final_part = types.Part(text="Answer body")
    fake_models = _FakeModels([
        ([fcall_part], _usage()),
        ([final_part], _usage()),
    ])
    monkeypatch.setattr(
        "google.genai.Client", lambda *a, **k: _FakeClient(fake_models)
    )

    events = await _drain(executor)
    kinds = [e.kind for e in events]

    assert EventKind.tool in kinds
    assert EventKind.result in kinds
    tokens = "".join(e.text or "" for e in events if e.kind == EventKind.token)
    assert "Answer body" in tokens

    result_ev = next(e for e in events if e.kind == EventKind.result)
    usage = result_ev.data["usage"]
    # Two rounds of usage accumulated; thoughts counted into output tokens.
    assert usage["tokens_in"] == 20
    assert usage["tokens_out"] == 16
    assert usage["cost_usd"] > 0


async def test_thought_text_is_not_surfaced_as_answer(gemini_executor):
    """`part.thought=True` parts must be replayed but never shown to the user."""
    executor, monkeypatch = gemini_executor

    thinking = types.Part(text="internal reasoning", thought=True)
    answer = types.Part(text="visible answer")
    fake_models = _FakeModels([([thinking, answer], _usage(thoughts=0))])
    monkeypatch.setattr(
        "google.genai.Client", lambda *a, **k: _FakeClient(fake_models)
    )

    events = await _drain(executor)
    tokens = "".join(e.text or "" for e in events if e.kind == EventKind.token)
    assert "visible answer" in tokens
    assert "internal reasoning" not in tokens


async def test_missing_credentials_errors_cleanly(monkeypatch):
    async def _no_key(*a, **k):
        return None

    monkeypatch.setattr("app.credentials.resolve_api_key", _no_key)
    pdef = get_provider_def("gemini-api")
    executor = ModelExecutor(pdef)
    events = [ev async for ev in executor.run_stream("x", workdir="/tmp")]
    assert events[-1].kind == EventKind.error
    assert "no credentials" in events[-1].message
