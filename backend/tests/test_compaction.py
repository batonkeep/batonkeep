"""
test_compaction.py — P-0048 levers 2+3: tool-result compaction.

The executor compacts aged tool results before re-sending the growing history,
protecting the most-recent tool turn and staying idempotent so settled history
re-caches (lever 1) instead of churning every round.
"""
from __future__ import annotations

from app.providers.model_executor import (
    _COMPACT_MARKER,
    _COMPACT_THRESHOLD_CHARS,
    _compact_anthropic_messages,
    _compact_gemini_contents,
    _compact_openai_messages,
    _compact_result_text,
)

_BIG = "x" * (_COMPACT_THRESHOLD_CHARS + 5000)
_SMALL = "y" * 50


def test_compact_result_text_threshold_and_idempotency():
    # Short text is left whole.
    assert _compact_result_text(_SMALL) == _SMALL
    # Large text is compacted and carries the marker + head/tail.
    out = _compact_result_text(_BIG)
    assert _COMPACT_MARKER in out
    assert len(out) < len(_BIG)
    assert out.startswith("x")
    assert out.endswith("x")
    # A second pass is a no-op (idempotent → byte-stable history).
    assert _compact_result_text(out) == out


def test_openai_protects_latest_turn():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "content": _BIG, "tool_call_id": "a"},          # aged
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "content": _BIG, "tool_call_id": "b"},          # latest
    ]
    _compact_openai_messages(messages)
    assert _COMPACT_MARKER in messages[3]["content"]      # aged → compacted
    assert messages[5]["content"] == _BIG                 # latest → verbatim
    # Idempotent across rounds.
    before = [m["content"] for m in messages]
    _compact_openai_messages(messages)
    assert [m["content"] for m in messages] == before


def test_anthropic_protects_latest_and_keeps_cache_control():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": _BIG}]},   # aged
        {"role": "assistant", "content": [{"type": "tool_use", "id": "b"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "b", "content": _BIG,
             "cache_control": {"type": "ephemeral"}}]},                       # latest
    ]
    _compact_anthropic_messages(messages)
    assert _COMPACT_MARKER in messages[2]["content"][0]["content"]
    assert messages[4]["content"][0]["content"] == _BIG
    # The breakpoint on the protected turn is untouched.
    assert messages[4]["content"][0]["cache_control"] == {"type": "ephemeral"}


class _FR:
    def __init__(self, result):
        self.response = {"result": result}


class _Part:
    def __init__(self, *, text=None, function_response=None):
        self.text = text
        self.function_response = function_response


class _Content:
    def __init__(self, role, parts):
        self.role = role
        self.parts = parts


def test_gemini_protects_latest_and_ignores_model_parts():
    contents = [
        _Content("user", [_Part(text="q")]),
        _Content("model", [_Part(text="t")]),
        _Content("user", [_Part(function_response=_FR(_BIG))]),   # aged
        _Content("model", [_Part(text="t2")]),
        _Content("user", [_Part(function_response=_FR(_BIG))]),   # latest
    ]
    _compact_gemini_contents(contents)
    assert _COMPACT_MARKER in contents[2].parts[0].function_response.response["result"]
    assert contents[4].parts[0].function_response.response["result"] == _BIG
