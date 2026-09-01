"""P-0106 / D-0069 — the version fence on parked-run checkpoints.

The fence is the entire reason storing opaque provider state is safe. These tests pin the
behaviour the decision turns on: a checkpoint written by a *different* provider, model, SDK
or format must **refuse to resume** rather than replay a stale `thought_signature` into an
opaque provider error nobody can trace back to its cause.

Strictness is the point. A checkpoint that *might* replay is worse than one that refuses.
"""
from __future__ import annotations

from app import checkpoint


def _cp(**over):
    cp = checkpoint.build(
        path="anthropic", provider="claude-api", model="claude-x",
        messages=[{"role": "user", "content": "hi"}],
        usage={"tokens_in": 1, "tokens_out": 2, "cost_usd": 0.5},
        round_num=2, tool_call={"id": "t1", "name": "code_exec", "args": "{}"},
    )
    cp["fence"].update(over.pop("fence", {}))
    cp.update(over)
    return cp


def test_a_matching_checkpoint_resumes():
    assert checkpoint.verify_fence(_cp(), provider="claude-api", model="claude-x") is None


def test_a_missing_checkpoint_is_refused():
    why = checkpoint.verify_fence(None, provider="claude-api", model="claude-x")
    assert why and "no checkpoint" in why


def test_a_different_provider_is_refused():
    why = checkpoint.verify_fence(_cp(), provider="grok-api", model="claude-x")
    assert why and "provider" in why


def test_a_different_model_is_refused():
    """Same provider, different model still means a different conversation contract."""
    why = checkpoint.verify_fence(_cp(), provider="claude-api", model="claude-y")
    assert why and "model" in why


def test_a_different_sdk_version_is_refused():
    """The case the fence exists for: images install SDKs at build time, so a rebuild
    can move them with no source change at all."""
    cp = _cp(fence={"sdk": "0.0.1-ancient"})
    why = checkpoint.verify_fence(cp, provider="claude-api", model="claude-x")
    assert why and "SDK" in why
    assert "thought signature" in why, "the reason should say why this is not portable"


def test_a_future_checkpoint_format_is_refused():
    cp = _cp(v=checkpoint.CHECKPOINT_VERSION + 1)
    why = checkpoint.verify_fence(cp, provider="claude-api", model="claude-x")
    assert why and "format" in why


def test_the_fence_records_a_real_sdk_version():
    """A fence stamped 'unknown' would silently match anything after an upgrade."""
    cp = _cp()
    assert cp["fence"]["sdk"] not in (None, "", "unknown")


def test_the_checkpoint_carries_what_resume_needs():
    cp = _cp()
    assert cp["messages"] == [{"role": "user", "content": "hi"}]
    assert cp["usage"]["cost_usd"] == 0.5, "usage must carry over or budget resets on resume"
    assert cp["tool_call"]["id"] == "t1", "the pending call must be identifiable on resume"


def test_sdk_objects_are_serialized_via_their_own_dict_form():
    """Provider SDKs hand back pydantic/protobuf wrappers, not plain dicts."""
    class Block:
        def model_dump(self):
            return {"type": "tool_use", "id": "t1", "name": "code_exec"}

    out = checkpoint.serialize_messages([{"role": "assistant", "content": [Block()]}])
    assert out == [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                                      "name": "code_exec"}]}]


def test_unserializable_state_raises_rather_than_dropping_it():
    """Silently dropping an unrepresentable field would resume into a provider error much
    later. The caller catches this and falls back to waiting in-process."""
    try:
        checkpoint.serialize_messages([{"role": "assistant", "content": object()}])
    except TypeError as exc:
        assert "cannot serialize" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a TypeError")


def test_the_fence_stamps_the_effective_model_not_the_template_default():
    """A near-inert fence, found on the testbed: the stamp must follow the model the
    request actually goes to, or an operator's per-instance override is invisible to it.

    The earlier version read the *template* default, so stamp and comparison came from the
    same near-constant — self-consistent, and therefore silently useless.
    """
    from app.providers.model_executor import ModelExecutor

    class _Def:
        model = "template-default"
        kind = "anthropic"

    ex = ModelExecutor.__new__(ModelExecutor)
    ex._def = _Def()
    ex._extra = {}
    ex._model = "instance-override"

    assert ex._checkpoint_model() == "instance-override", (
        "the fence must follow the effective model, not the template default"
    )
