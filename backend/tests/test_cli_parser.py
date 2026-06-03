"""
tests/test_cli_parser.py — P4 gate: parser unit tests on NDJSON/JSON/text fixtures.

No live agent needed. Tests the tolerant parse_line() function against
captured real-world output shapes from claude/grok/agy.
"""
from __future__ import annotations

import json
import pytest
from app.providers.cli_executor import parse_line, _is_rate_limit, _parse_reset_at
from app.providers.base import EventKind


# ── Fixtures ──────────────────────────────────────────────────────────────────

# Claude stream-json format (NDJSON lines)
CLAUDE_NDJSON_LINES = [
    '{"type":"assistant","message":{"id":"msg_01","type":"message","role":"assistant","content":[{"type":"text","text":"Hello, I am Claude."}],"model":"claude-opus-4-5","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":8}}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" More text."}}}',
    '{"type":"tool_use","id":"toolu_01","name":"web_search","input":{"query":"AI news"}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"Final answer here.","session_id":"sess_01","cost_usd":0.0025,"duration_ms":3200,"num_turns":2,"usage":{"input_tokens":150,"output_tokens":80}}',
]

# Grok streaming-json format (actual output from grok 0.2.14 --output-format streaming-json).
# Deltas use {"type":"text","data":"..."}, terminal is {"type":"end","stopReason":"EndTurn",...}.
# There is no separate type:"result" event; the full report is assembled from the deltas.
GROK_JSON_LINES = [
    '{"type":"text","data":"Starting research"}',
    '{"type":"text","data":", and preprints"}',
    '{"type":"text","data":".\\n"}',
    '{"type":"end","stopReason":"EndTurn","sessionId":"019e82a6-b49e-7d30-b69a-fce932d1b263","requestId":"ab4c2789-53c3-408f-94d0-d762e73df6eb"}',
]

# agy single-object JSON (whole output is one JSON blob)
AGY_SINGLE_JSON = '{"type":"result","result":"Antigravity answer","cost_usd":0.0,"usage":{}}'

# Plain text fallback (no JSON at all)
PLAIN_TEXT_LINES = [
    "This is the first line of the report.",
    "## Section Two",
    "Some analysis here.",
]

# Rate-limit fixtures
RATE_LIMIT_MESSAGES = [
    "Error: rate limit exceeded, resets at 2026-06-01T18:00:00Z",
    "Claude: Usage limit reached. Try again in 300 seconds.",
    "too many requests - quota exceeded",
    "Error: tokens per hour limit hit. Wait 120 seconds.",
]


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestClaudeNDJSON:
    def test_assistant_block_is_suppressed_as_log(self):
        """type:'assistant' is now a log event (not token) — it's a duplicate of
        the stream_event deltas that were already streamed and accumulated."""
        buf: list[str] = []
        ev = parse_line(CLAUDE_NDJSON_LINES[0], buf)
        assert ev is not None
        assert ev.kind == EventKind.log   # not token — suppressed to avoid duplicate text
        assert len(buf) == 0              # nothing added to accumulated_text

    def test_stream_event_delta_yields_token(self):
        buf: list[str] = []
        ev = parse_line(CLAUDE_NDJSON_LINES[1], buf)
        assert ev is not None
        assert ev.kind == EventKind.token
        assert " More text." in (ev.text or "")

    def test_tool_use_yields_tool_event(self):
        buf: list[str] = []
        ev = parse_line(CLAUDE_NDJSON_LINES[2], buf)
        assert ev is not None
        assert ev.kind == EventKind.tool
        assert ev.message == "web_search"

    def test_result_has_exec_result_in_data(self):
        """Result event must carry an ExecResult in data['result'] so the
        orchestrator can use it directly without needing fallback synthesis."""
        from app.providers.base import ExecResult
        buf: list[str] = []
        ev = parse_line(CLAUDE_NDJSON_LINES[3], buf)
        assert ev is not None
        assert ev.kind == EventKind.result
        assert ev.is_terminal()
        assert isinstance(ev.data["result"], ExecResult)
        assert ev.data["result"].text == "Final answer here."
        usage = ev.data["usage"]
        assert usage["tokens_in"] == 150
        assert usage["tokens_out"] == 80
        assert abs(usage["cost_usd"] - 0.0025) < 1e-9

    def test_result_does_not_append_to_accumulated_text(self):
        """The result handler must NOT append to accumulated_text — streaming
        deltas already have the text there, and appending would duplicate it."""
        buf: list[str] = []
        # process the delta first so accumulated_text has the streamed text
        parse_line(CLAUDE_NDJSON_LINES[1], buf)  # stream_event delta → " More text."
        before = list(buf)
        parse_line(CLAUDE_NDJSON_LINES[3], buf)  # result → must NOT append again
        assert buf == before  # unchanged

    def test_stream_event_delta_accumulates_text(self):
        buf: list[str] = []
        parse_line(CLAUDE_NDJSON_LINES[1], buf)
        assert " More text." in "".join(buf)


class TestGrokJSON:
    def test_text_delta_yields_token(self):
        buf: list[str] = []
        ev = parse_line(GROK_JSON_LINES[0], buf)
        assert ev is not None and ev.kind == EventKind.token
        assert ev.text == "Starting research"

    def test_deltas_accumulate_into_full_text(self):
        buf: list[str] = []
        for line in GROK_JSON_LINES[:3]:
            parse_line(line, buf)
        assert "".join(buf) == "Starting research, and preprints.\n"

    def test_end_event_is_detectable_log(self):
        """type:'end' must come through as log with data.type=='end' so the executor breaks."""
        buf: list[str] = []
        ev = parse_line(GROK_JSON_LINES[3], buf)
        assert ev is not None
        assert ev.kind == EventKind.log
        assert ev.data.get("type") == "end"
        assert ev.data.get("stopReason") == "EndTurn"


class TestAgySingleJSON:
    def test_single_json_blob_is_result(self):
        buf: list[str] = []
        ev = parse_line(AGY_SINGLE_JSON, buf)
        assert ev is not None
        assert ev.kind == EventKind.result
        assert ev.is_terminal()


class TestPlainText:
    def test_non_json_lines_become_tokens(self):
        buf: list[str] = []
        events = [parse_line(line, buf) for line in PLAIN_TEXT_LINES]
        assert all(e is not None and e.kind == EventKind.token for e in events)
        full = "".join(buf)
        assert "Section Two" in full

    def test_empty_line_returns_none(self):
        buf: list[str] = []
        ev = parse_line("", buf)
        assert ev is None

    def test_whitespace_only_returns_none(self):
        buf: list[str] = []
        ev = parse_line("   \n", buf)
        assert ev is None


class TestRateLimitDetection:
    @pytest.mark.parametrize("msg", RATE_LIMIT_MESSAGES)
    def test_rate_limit_detected(self, msg: str):
        assert _is_rate_limit(msg), f"Failed to detect rate-limit in: {msg}"

    def test_normal_text_not_rate_limit(self):
        assert not _is_rate_limit("Successfully completed the research task.")

    def test_parse_reset_iso_timestamp(self):
        ts = _parse_reset_at("rate limit exceeded, resets at 2026-06-01T18:00:00Z")
        assert ts is not None
        assert ts.year == 2026
        assert ts.month == 6

    def test_parse_reset_seconds(self):
        ts = _parse_reset_at("Try again in 120 seconds")
        assert ts is not None
        # Should be ~2 minutes in the future
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        diff = (ts - now).total_seconds()
        assert 100 < diff < 140  # allow slight timing drift

    def test_default_cooldown_on_no_timestamp(self):
        ts = _parse_reset_at("rate limit reached")
        assert ts is not None  # returns default cooldown


# ── Claude stream-json v2 format (type:"text" + type:"end") ──────────────────

CLAUDE_V2_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess_01","tools":[]}',
    '{"type":"text","data":"Hello, "}',
    '{"type":"text","data":"world"}',
    '{"type":"text","data":"."}',
    '{"type":"end","stopReason":"EndTurn","sessionId":"sess_01","requestId":"req_01"}',
]


class TestClaudeStreamV2:
    def test_text_delta_yields_token(self):
        buf: list[str] = []
        ev = parse_line(CLAUDE_V2_LINES[1], buf)
        assert ev is not None
        assert ev.kind == EventKind.token
        assert ev.text == "Hello, "

    def test_text_delta_accumulates(self):
        buf: list[str] = []
        for line in CLAUDE_V2_LINES[1:4]:
            parse_line(line, buf)
        assert "".join(buf) == "Hello, world."

    def test_end_event_is_log_with_data(self):
        buf: list[str] = []
        ev = parse_line(CLAUDE_V2_LINES[4], buf)
        assert ev is not None
        assert ev.kind == EventKind.log
        assert isinstance(ev.data, dict) and ev.data.get("type") == "end"

    def test_system_init_event_does_not_crash(self):
        buf: list[str] = []
        ev = parse_line(CLAUDE_V2_LINES[0], buf)
        # Should return some event (log) without raising
        assert ev is None or ev.kind == EventKind.log


# ── Codex exec --json JSONL format (codex-cli 0.136+) ────────────────────────

CODEX_JSON_LINES = [
    '{"type":"thread.started","thread_id":"019e8676-7183-79a3-ae01-5edb18588328"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Intermediate reasoning step."}}',
    '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"ls /tmp","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"ls /tmp","aggregated_output":"file.txt\\n","exit_code":0,"status":"completed"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"The final report is here."}}',
    '{"type":"turn.completed","usage":{"input_tokens":11813,"cached_input_tokens":2432,"output_tokens":34,"reasoning_output_tokens":26}}',
]


class TestCodexJSON:
    def test_thread_started_is_log(self):
        buf: list[str] = []
        ev = parse_line(CODEX_JSON_LINES[0], buf)
        assert ev is not None and ev.kind == EventKind.log

    def test_agent_message_yields_token(self):
        buf: list[str] = []
        ev = parse_line(CODEX_JSON_LINES[2], buf)
        assert ev is not None
        assert ev.kind == EventKind.token
        assert ev.text == "Intermediate reasoning step."

    def test_later_agent_message_replaces_accumulated(self):
        """Only the last agent_message should remain in accumulated_text."""
        buf: list[str] = []
        parse_line(CODEX_JSON_LINES[2], buf)  # intermediate
        parse_line(CODEX_JSON_LINES[5], buf)  # final
        assert "".join(buf) == "The final report is here."

    def test_command_started_is_tool_event(self):
        buf: list[str] = []
        ev = parse_line(CODEX_JSON_LINES[3], buf)
        assert ev is not None and ev.kind == EventKind.tool
        assert "ls /tmp" in ev.message

    def test_command_completed_is_tool_event(self):
        buf: list[str] = []
        ev = parse_line(CODEX_JSON_LINES[4], buf)
        assert ev is not None and ev.kind == EventKind.tool
        assert ev.data.get("exit_code") == 0

    def test_turn_completed_yields_result_with_exec_result(self):
        from app.providers.base import ExecResult
        buf: list[str] = []
        # Seed accumulated text so the result has content
        parse_line(CODEX_JSON_LINES[5], buf)
        ev = parse_line(CODEX_JSON_LINES[6], buf)
        assert ev is not None
        assert ev.kind == EventKind.result
        assert ev.is_terminal()
        result = ev.data["result"]
        assert isinstance(result, ExecResult)
        assert result.text == "The final report is here."
        # usage: input_tokens + cached_input_tokens
        assert ev.data["usage"]["tokens_in"] == 11813 + 2432
        assert ev.data["usage"]["tokens_out"] == 34
