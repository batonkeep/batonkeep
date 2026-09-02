"""
test_openai_responses_executor.py — the `/v1/responses` lane ([[D-0074]]).

Why this lane exists: OpenAI's reasoning class (`gpt-5.6-terra` and after) **rejects
function tools on `/v1/chat/completions`**, because the model's server-side default sets
`reasoning_effort` and we never send it. Found live on Runtime B — `gpt-4.1` worked, so
the lane was fine and the model class was not. The two cheap fixes were both capability
losses (sending `reasoning_effort: "none"` silently downgrades a reasoning model; an
allowlist only makes the failure honest), so D-0074 adopted Responses.

The load-bearing tests here are the round-trip of opaque `reasoning` items — the exact
shape of failure `_run_gemini` exists for (D-0034/P-0043) — and the negative one: that we
still never send `reasoning_effort`, which is the whole defect.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.providers.base import EventKind
from app.providers.model_executor import ModelExecutor, _compact_responses_input
from app.providers.registry import get_provider_def


def _usage(inp=100, out=20, cached=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


class _Item:
    """A stand-in for an SDK output item. It carries `model_dump` on purpose: the real
    items are pydantic models, and that method is the only thing the checkpoint
    serializer knows how to ask them for."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self):
        # Nulls included, exactly as pydantic's does — that is the whole hazard.
        return dict(self.__dict__)


def _reasoning(enc="ENCRYPTED-CHAIN", **kw):
    """A reasoning item as the SDK hands it over — `status` included, because that is
    the field whose null form the API rejects on echo-back."""
    return _Item(type="reasoning", id="rs_1", encrypted_content=enc, summary=[],
                 **({"status": kw["status"]} if "status" in kw else {}))


def _call(name="code_exec", args='{"code":"1"}', cid="call_1"):
    return _Item(type="function_call", call_id=cid, name=name, arguments=args)


def _text_item(text="done"):
    return _Item(type="message", role="assistant", content=[{"type": "output_text",
                                                             "text": text}])


class _FakeResponses:
    """Replays scripted rounds and snapshots the kwargs each call was handed, so the
    request shape and the conversation round-trip can both be asserted."""

    def __init__(self, rounds):
        self._rounds = rounds
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        text, output, usage = self._rounds[min(len(self.calls) - 1, len(self._rounds) - 1)]

        async def _gen():
            if text:
                yield SimpleNamespace(type="response.output_text.delta", delta=text)
            yield SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(output=output, usage=usage),
            )

        return _gen()


@pytest.fixture
def responses_executor(monkeypatch, tmp_path):
    async def _fake_key(*a, **k):
        return "fake-key"

    monkeypatch.setattr("app.credentials.resolve_api_key", _fake_key)

    async def _fake_tool(self, name, args_json, *, workdir):
        return f"[tool {name} result]"

    monkeypatch.setattr(ModelExecutor, "_call_tool", _fake_tool)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    pdef = get_provider_def("openai-api")
    assert pdef is not None and pdef.api_shape == "responses"

    def _make(rounds):
        fake = _FakeResponses(rounds)

        class _FakeClient:
            def __init__(self, **kw):
                self.responses = fake

        monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)
        return ModelExecutor(pdef), fake

    return _make, str(tmp_path)


async def _drive(ex, workdir, **kw):
    events = []
    async for ev in ex.run_stream("do the thing", workdir=workdir, max_rounds=4,
                                  budget_usd=10.0, **kw):
        events.append(ev)
    return events


# ── which shape a provider speaks ────────────────────────────────────────────

class TestShapeResolution:
    def test_openai_speaks_responses(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert ModelExecutor(get_provider_def("openai-api"))._api_shape() == "responses"

    def test_a_redirected_openai_provider_falls_back_to_chat(self, monkeypatch):
        """`OPENAI_BASE_URL` is a documented deployment knob — a proxy, a gateway, a
        compat shim. An operator who set it is by definition not talking to OpenAI, and
        almost nothing else implements /v1/responses, so the capability is what gives
        way rather than every run."""
        monkeypatch.setenv("OPENAI_BASE_URL", "http://litellm.internal:4000/v1")
        assert ModelExecutor(get_provider_def("openai-api"))._api_shape() == "chat"

    def test_an_explicit_openai_base_url_still_speaks_responses(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        assert ModelExecutor(get_provider_def("openai-api"))._api_shape() == "responses"

    @pytest.mark.parametrize("name", ["grok-api", "ollama", "open-default"])
    def test_every_other_openai_compatible_provider_stays_on_chat(self, name, monkeypatch):
        """Replacing the chat loop was never available: `openai_compatible` is also
        xAI, Ollama, vLLM and OpenRouter, and none of them implement /v1/responses."""
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert ModelExecutor(get_provider_def(name))._api_shape() == "chat"

    def test_a_custom_provider_inherits_chat(self):
        """Operator-declared endpoints are compat endpoints; the safe default is the
        one that keeps them working."""
        from app.custom_providers import CustomProvider
        pdef = CustomProvider(id="mine", label="Mine", base_url="http://x/v1",
                              default_model="m").to_provider_def()
        assert pdef.api_shape == "chat"
        assert ModelExecutor(pdef)._api_shape() == "chat"


# ── the request shape ────────────────────────────────────────────────────────

class TestRequestShape:
    async def test_never_sends_reasoning_effort(self, responses_executor):
        """The defect D-0074 exists for. Sending it downgrades a reasoning model to
        whatever we guessed, which is the quiet capability loss D-0049 retreated from —
        the server-side default is the capability we are keeping."""
        make, wd = responses_executor
        ex, fake = make([("answer", [_text_item()], _usage())])
        await _drive(ex, wd)
        assert all("reasoning_effort" not in c for c in fake.calls)

    async def test_tools_are_flat_not_nested(self, responses_executor):
        """Responses takes function tools flat. The chat shape (`{"type":"function",
        "function":{…}}`) is accepted by the SDK and rejected by the API, so this
        difference is load-bearing and invisible until it 400s."""
        make, wd = responses_executor
        ex, fake = make([("answer", [_text_item()], _usage())])
        await _drive(ex, wd)
        tools = fake.calls[0]["tools"]
        assert tools and all("function" not in t for t in tools)
        assert all({"type", "name", "parameters"} <= set(t) for t in tools)

    async def test_conversation_state_stays_on_our_side(self, responses_executor):
        """No `previous_response_id`: it would make OpenAI the holder of the
        transcript, which breaks sovereignty (P-0009), P-0106 resumption (the fence
        would depend on their retention, not ours) and cross-provider switching
        (D-0008). `store=False` + encrypted reasoning is the shape that satisfies all."""
        make, wd = responses_executor
        ex, fake = make([("answer", [_text_item()], _usage())])
        await _drive(ex, wd)
        call = fake.calls[0]
        assert call["store"] is False
        assert "previous_response_id" not in call
        assert "reasoning.encrypted_content" in call["include"]

    async def test_the_system_prompt_is_instructions_not_history(self, responses_executor):
        """Keeping it out of the item list means a resumed conversation cannot replay a
        stale system prompt — the `messages[0] = …` rewrite the chat loop needs."""
        make, wd = responses_executor
        ex, fake = make([("answer", [_text_item()], _usage())])
        await _drive(ex, wd)
        assert fake.calls[0]["instructions"]
        assert all(
            (it.get("role") if isinstance(it, dict) else None) != "system"
            for it in fake.calls[0]["input"]
        )


# ── the round-trip that matters ──────────────────────────────────────────────

class TestReasoningRoundTrip:
    async def test_reasoning_items_are_echoed_back(self, responses_executor):
        """The Gemini `thought_signature` failure in a different costume: the model's
        own opaque payload has to come back on the next round, or multi-step tool use
        degrades or 400s on round two. Verified against `gpt-5.6-terra` on Runtime B —
        a real turn returns a 1292-byte `encrypted_content` on its reasoning item."""
        make, wd = responses_executor
        ex, fake = make([
            ("", [_reasoning(), _call()], _usage()),
            ("final", [_text_item("final")], _usage()),
        ])
        await _drive(ex, wd)
        echoed = [it for it in fake.calls[1]["input"]
                  if isinstance(it, dict) and it.get("type") == "reasoning"]
        assert echoed and echoed[0]["encrypted_content"] == "ENCRYPTED-CHAIN"

    async def test_output_only_fields_are_stripped_before_echoing(self, responses_executor):
        """The live defect ([[DRILL-D0074-R3]]). `model_dump()` emits `"status": null` on
        a reasoning item and the API rejects it — `Unknown parameter: 'input[1].status'`.

        The SDK strips it when handed its own objects, so the **live** path would have
        worked and only a **resumed** one would have failed: a checkpoint stores exactly
        the `model_dump()` shape. Parking on this lane would have been a one-way door.
        """
        make, wd = responses_executor
        ex, fake = make([
            ("", [_reasoning(status=None), _call(), _text_item()], _usage()),
            ("final", [_text_item("final")], _usage()),
        ])
        await _drive(ex, wd)
        for it in fake.calls[1]["input"]:
            assert not (isinstance(it, dict) and "status" in it), (
                "a null output-only field must never be echoed back"
            )

    async def test_live_and_resumed_rounds_send_the_same_shape(self, responses_executor):
        """The reason the fix normalizes at the *source*: if a live round sent SDK objects
        and a resumed round sent checkpoint dicts, the two would diverge and only the
        resume would be wrong — which is the failure mode no unit test can see, because
        the fake defines the shape it then asserts on."""
        from app import checkpoint

        make, wd = responses_executor
        ex, fake = make([
            ("", [_reasoning(), _call()], _usage()),
            ("final", [_text_item("final")], _usage()),
        ])
        await _drive(ex, wd)
        sent = fake.calls[1]["input"]
        assert checkpoint.serialize_messages(sent) == sent, (
            "what a live round sends must survive a checkpoint round-trip unchanged"
        )

    async def test_the_tool_result_uses_the_responses_shape(self, responses_executor):
        make, wd = responses_executor
        ex, fake = make([
            ("", [_reasoning(), _call()], _usage()),
            ("final", [_text_item("final")], _usage()),
        ])
        events = await _drive(ex, wd)
        outputs = [it for it in fake.calls[1]["input"]
                   if isinstance(it, dict) and it.get("type") == "function_call_output"]
        assert len(outputs) == 1
        assert outputs[0]["call_id"] == "call_1"
        assert "[tool code_exec result]" in outputs[0]["output"]
        assert any(e.kind == EventKind.tool for e in events)

    async def test_the_answer_is_the_last_turns_text(self, responses_executor):
        make, wd = responses_executor
        ex, fake = make([
            ("thinking out loud…", [_reasoning(), _call()], _usage()),
            ("the actual answer", [_text_item()], _usage()),
        ])
        events = await _drive(ex, wd)
        result = [e for e in events if e.kind == EventKind.result][-1]
        assert result.data["result"].text == "the actual answer"


# ── metering ─────────────────────────────────────────────────────────────────

class TestUsage:
    async def test_cached_input_is_not_billed_twice(self, responses_executor):
        """Responses reports `input_tokens` *including* the cached portion, as
        chat/completions does — so it has to be split out or the prefix bills twice."""
        make, wd = responses_executor
        ex, fake = make([("answer", [_text_item()], _usage(inp=1000, out=50, cached=800))])
        events = await _drive(ex, wd)
        usage = [e for e in events if e.kind == EventKind.result][-1].data["usage"]
        assert usage["tokens_in"] == 200
        assert usage["cache_read_tokens"] == 800


# ── P-0106: a fourth shape is a fourth checkpoint path ───────────────────────

class TestCheckpointPath:
    async def test_parking_stamps_the_responses_path(self, responses_executor, monkeypatch):
        make, wd = responses_executor
        ex, fake = make([
            ("", [_reasoning(), _call()], _usage()),
            ("final", [_text_item()], _usage()),
        ])
        seen: dict = {}

        async def _park(self, name, args_json, *, workdir):
            seen["cp"] = self._extra["_checkpoint"]()
            raise __import__("app.checkpoint", fromlist=["x"]).ParkRequested("req-1")

        monkeypatch.setattr(ModelExecutor, "_call_tool", _park)
        events = await _drive(ex, wd)
        assert any(e.kind == EventKind.parked for e in events)
        assert seen["cp"]["fence"]["path"] == "openai_responses"

    def test_a_chat_checkpoint_does_not_resume_this_lane(self, responses_executor):
        """Same SDK, different conversation representation — replaying one into the
        other would hand the API a shape it has never seen. `_resume_state` keys on the
        path, so the mismatch simply does not resume."""
        make, _ = responses_executor
        ex, _fake = make([])
        ex._extra = {"resume": {"fence": {"path": "openai_compat"}, "messages": []}}
        assert ex._resume_state("openai_responses") is None
        assert ex._resume_state("openai_compat") is not None

    def test_the_fence_knows_the_responses_sdk(self):
        from app import checkpoint
        assert checkpoint._SDK_DIST["openai_responses"] == "openai"
        assert checkpoint._sdk_version("openai_responses") != "unknown"


# ── compaction ───────────────────────────────────────────────────────────────

class TestCompaction:
    def _long(self):
        return "x" * 60000

    def test_aged_outputs_are_compacted_and_the_last_turn_is_not(self):
        items = [
            {"type": "function_call", "call_id": "1"},
            {"type": "function_call_output", "call_id": "1", "output": self._long()},
            {"type": "function_call", "call_id": "2"},
            {"type": "function_call_output", "call_id": "2", "output": self._long()},
        ]
        _compact_responses_input(items)
        assert len(items[1]["output"]) < 60000, "the aged turn is compacted"
        assert len(items[3]["output"]) == 60000, "the most-recent turn stays verbatim"

    def test_model_items_are_never_rewritten(self):
        """Same rule as `_compact_gemini_contents`: the model's opaque payload has to
        come back byte-identical, so we only ever touch outputs we wrote ourselves."""
        r = _reasoning("x" * 60000)
        items = [r, {"type": "function_call", "call_id": "1"},
                 {"type": "function_call_output", "call_id": "1", "output": "short"}]
        _compact_responses_input(items)
        assert r.encrypted_content == "x" * 60000

    def test_it_is_idempotent(self):
        items = [
            {"type": "function_call_output", "call_id": "1", "output": self._long()},
            {"type": "function_call", "call_id": "2"},
        ]
        _compact_responses_input(items)
        once = items[0]["output"]
        _compact_responses_input(items)
        assert items[0]["output"] == once
