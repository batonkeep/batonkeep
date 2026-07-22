"""P-0082: the plan/CLI lane's transport to the planner tools.

Before this, `CLIExecutor` never consulted the tool registry, so a planning turn
on a `cli` provider was offered no tools at all — six consecutive runs on the live
instance produced zero structural output and five recorded `succeeded`.

These cover the transport, not planning semantics: the semantics are the same
`planner_tools` functions both lanes dispatch into, and testing them twice would
be testing the thing that is deliberately shared.
"""
from __future__ import annotations

import pytest

from app.providers import planner_protocol as pp


class TestExtractCalls:
    def test_reads_a_block_surrounded_by_prose(self):
        """The realistic shape: a model explains itself, then records."""
        text = (
            "Looking at the ledger, WI-9 is the only thing open.\n\n"
            "```batonkeep-plan\n"
            '[{"tool": "summarize_project", "args": {"headline": "one item open"}}]\n'
            "```\n"
            "Happy to go deeper on any of these.\n"
        )
        calls, err = pp.extract_calls(text)
        assert err is None
        assert calls == [{"tool": "summarize_project",
                          "args": {"headline": "one item open"}}]

    def test_no_block_is_not_an_error(self):
        """A turn that emitted nothing is a real outcome (P-0080's `no_proposals`),
        distinct from a turn that tried and produced garbage."""
        calls, err = pp.extract_calls("Here is my plan, in prose, with no block.")
        assert calls == []
        assert err is None

    def test_malformed_block_reports_rather_than_silently_dropping(self):
        text = "```batonkeep-plan\n[{'tool': broken,,}]\n```"
        calls, err = pp.extract_calls(text)
        assert calls == []
        assert err and "not valid JSON" in err

    def test_tolerates_a_trailing_comma(self):
        """The most common hand-written-JSON error models make. Refusing a plan
        over one character would be pedantry, not safety."""
        text = ('```batonkeep-plan\n'
                '[{"tool": "triage_signal", "args": {"title": "x",}},]\n```')
        calls, err = pp.extract_calls(text)
        assert err is None
        assert calls[0]["tool"] == "triage_signal"

    def test_accepts_a_single_object_instead_of_an_array(self):
        text = '```batonkeep-plan\n{"tool": "triage_signal", "args": {"title": "x"}}\n```'
        calls, err = pp.extract_calls(text)
        assert err is None
        assert len(calls) == 1

    def test_accepts_name_and_arguments_aliases(self):
        """Models trained on the OpenAI tool shape reach for `name`/`arguments`."""
        text = ('```batonkeep-plan\n'
                '[{"name": "triage_signal", "arguments": {"title": "x"}}]\n```')
        calls, err = pp.extract_calls(text)
        assert err is None
        assert calls == [{"tool": "triage_signal", "args": {"title": "x"}}]

    def test_last_block_wins_when_a_model_revises_itself(self):
        text = ('```batonkeep-plan\n[{"tool": "a", "args": {}}]\n```\n'
                "on reflection:\n"
                '```batonkeep-plan\n[{"tool": "b", "args": {}}]\n```')
        calls, err = pp.extract_calls(text)
        assert [c["tool"] for c in calls] == ["b"]

    def test_call_without_a_tool_name_is_refused(self):
        text = '```batonkeep-plan\n[{"args": {"title": "x"}}]\n```'
        calls, err = pp.extract_calls(text)
        assert calls == []
        assert "no tool name" in err

    def test_non_object_args_are_refused(self):
        text = '```batonkeep-plan\n[{"tool": "triage_signal", "args": "oops"}]\n```'
        calls, err = pp.extract_calls(text)
        assert calls == []
        assert "non-object args" in err

    def test_missing_args_defaults_to_empty(self):
        text = '```batonkeep-plan\n[{"tool": "summarize_project"}]\n```'
        calls, err = pp.extract_calls(text)
        assert err is None
        assert calls[0]["args"] == {}

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_input_is_safe(self, text):
        assert pp.extract_calls(text) == ([], None)


class TestStripBlock:
    def test_prose_survives_and_the_block_does_not(self):
        """The prose is often the most useful thing a planning turn produces — it is
        kept and shown, it is simply not the record."""
        text = ("Three things stand out.\n\n"
                '```batonkeep-plan\n[{"tool": "x", "args": {}}]\n```\n\nHope that helps.')
        out = pp.strip_block(text)
        assert "Three things stand out." in out
        assert "Hope that helps." in out
        assert "batonkeep-plan" not in out and '"tool"' not in out


class TestGeneratedInstructions:
    """The contract text is generated from the same schemas the API lane offers, so
    the transports cannot drift. These assert the generation, not the wording."""

    def _schemas(self, scope_item: bool):
        from app.planner import _protocol_schemas
        return _protocol_schemas(1 if scope_item else None)

    def test_describes_every_tool_the_scope_offers(self):
        from app.providers.tools.registry import PLANNER_PROJECT_TOOL_NAMES

        text = pp.protocol_instructions(self._schemas(scope_item=False))
        for name in PLANNER_PROJECT_TOOL_NAMES:
            assert f"`{name}`" in text

    def test_scopes_match_the_api_lane_split(self):
        from app.providers.tools.registry import (
            PLANNER_ITEM_TOOL_NAMES, PLANNER_PROJECT_TOOL_NAMES,
        )

        item = {s["name"] for s in self._schemas(scope_item=True)}
        project = {s["name"] for s in self._schemas(scope_item=False)}
        assert item == set(PLANNER_ITEM_TOOL_NAMES)
        assert project == set(PLANNER_PROJECT_TOOL_NAMES)
        assert not item & project

    def test_required_arguments_are_marked(self):
        text = pp.protocol_instructions(self._schemas(scope_item=False))
        assert "required" in text and "optional" in text

    def test_tells_the_model_that_nothing_to_do_still_needs_recording(self):
        """Without this the fix trades a false `succeeded` for a false
        `no_proposals`: a planner that correctly finds nothing wrong would look
        like one that failed to produce anything."""
        text = pp.protocol_instructions(self._schemas(scope_item=False))
        assert "nothing needs doing" in text

    def test_no_schemas_means_no_instructions(self):
        assert pp.protocol_instructions([]) == ""


class TestProviderRouting:
    """Which lane a provider takes is keyed on `kind`, not a name list, so a new
    CLI provider inherits the protocol instead of silently planning into the void."""

    def test_cli_providers_use_the_protocol(self):
        from app.planner import _uses_protocol
        assert _uses_protocol("grok") is True
        assert _uses_protocol("agy") is True

    def test_api_providers_use_native_tool_calling(self):
        from app.planner import _uses_protocol
        assert _uses_protocol("claude-api") is False
        assert _uses_protocol("ollama") is False

    def test_unknown_provider_does_not_crash(self):
        from app.planner import _uses_protocol
        assert _uses_protocol("nope-not-a-provider") is False
