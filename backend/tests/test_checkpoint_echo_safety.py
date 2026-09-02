"""A checkpoint must store what the provider will accept back ([[DRILL-PARK-R6]]).

Found on Runtime B, not by any test. An Anthropic assistant turn that **narrates before
calling the tool** carries a `text` block alongside the `tool_use`, and that block's
`model_dump()` includes `"citations": null, "parsed_output": null` — output-only fields the
Messages API rejects on echo-back:

    messages.1.content.0.text.parsed_output: Extra inputs are not permitted

The live path never sees this: the loop appends the SDK's own objects and the SDK strips
them. Only a **resumed** run replays checkpoint dicts, so Gate B's restart-survival
guarantee was broken on the Anthropic lane for any run where the model said something
before calling the tool — which is most of them. `DRILL-PARK-R3a` passed only because
haiku happened to emit a bare `tool_use` that time.

Same root cause as [[D-0074]]'s `DRILL-D0074-R3` on the Responses lane, one lane over.
"""
from __future__ import annotations

from app import checkpoint


class _Block:
    """Stands in for an SDK content block — `model_dump` includes nulls, as pydantic's does."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self):
        return dict(self.__dict__)


class TestNullFieldsAreNotStored:
    def test_an_anthropic_text_block_loses_its_output_only_nulls(self):
        """The exact shape that 400'd on the testbed."""
        msgs = [{"role": "assistant", "content": [
            _Block(type="text", text="Let me compute that.", citations=None,
                   parsed_output=None),
            _Block(type="tool_use", id="tu_1", name="code_exec",
                   input={"code": "print(6*7)"}, caller=None),
        ]}]
        out = checkpoint.serialize_messages(msgs)
        text_block = out[0]["content"][0]
        assert "parsed_output" not in text_block
        assert "citations" not in text_block
        assert text_block["text"] == "Let me compute that."
        assert "caller" not in out[0]["content"][1]

    def test_the_approved_tool_arguments_are_never_rewritten(self):
        """The carve-out that makes null-stripping safe.

        A tool call's own payload is **the action the operator approved**. Stripping a null
        from inside it would resume a different call than the one adjudicated — which is
        precisely the substitution D-0069 refuses, arriving through the back door.
        """
        msgs = [{"role": "assistant", "content": [
            _Block(type="tool_use", id="tu_1", name="code_exec",
                   input={"code": "print(1)", "label": None, "timeout": None}),
        ]}]
        out = checkpoint.serialize_messages(msgs)
        assert out[0]["content"][0]["input"] == {
            "code": "print(1)", "label": None, "timeout": None,
        }

    def test_gemini_function_args_are_left_alone_too(self):
        """`args` is Gemini's payload key and `response` its result key — same rule."""
        msgs = [{"role": "model", "parts": [
            {"function_call": {"name": "f", "args": {"x": None}}, "thought_signature": "SIG"},
            {"function_response": {"name": "f", "response": {"result": None}}},
        ]}]
        out = checkpoint.serialize_messages(msgs)
        assert out[0]["parts"][0]["function_call"]["args"] == {"x": None}
        assert out[0]["parts"][1]["function_response"]["response"] == {"result": None}
        assert out[0]["parts"][0]["thought_signature"] == "SIG"

    def test_openai_chat_messages_are_unharmed(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": '{"a": null}'}}]},
            {"role": "tool", "content": "result", "tool_call_id": "c1"},
        ]
        out = checkpoint.serialize_messages(msgs)
        assert out == msgs, "a lane that builds its own dicts must round-trip identically"

    def test_it_is_idempotent(self):
        msgs = [{"role": "assistant", "content": [
            _Block(type="text", text="hi", parsed_output=None)]}]
        once = checkpoint.serialize_messages(msgs)
        assert checkpoint.serialize_messages(once) == once
