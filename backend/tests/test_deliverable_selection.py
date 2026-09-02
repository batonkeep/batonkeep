"""The projected work ledger must never become a run's deliverable.

`project_context` writes `WORKITEM.md` into the workdir as *input context*, and
`_find_best_agent_md` scans that same workdir for the largest agent-written `.md`. On a run
whose final text is short, the ledger wins — so the run returns the context it was handed
instead of the model's answer.

Observed live on Runtime B: identical prompts returned the report on `openai-api` (long
final text, so the size test failed) and the bare ledger on `claude-api` (81 output
tokens). A control run with parking disabled reproduced it, ruling out the resume path.
"""
from __future__ import annotations

import os

from app.orchestrator import _find_best_agent_md
from app.work_ledger import LEDGER_FILENAME


def _write(d, name, text):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


def test_the_projected_ledger_is_never_chosen(tmp_path):
    """Even when it is the only .md, and by far the largest."""
    _write(tmp_path, LEDGER_FILENAME, "# Work ledger — Proj\n" + ("ledger body\n" * 200))
    assert _find_best_agent_md(str(tmp_path)) is None


def test_a_real_report_wins_over_a_bigger_ledger(tmp_path):
    """The failure as it actually happened: the ledger dwarfs a short answer."""
    _write(tmp_path, LEDGER_FILENAME, "# Work ledger — Proj\n" + ("x\n" * 500))
    _write(tmp_path, "output.md", "# Report\n\n42\n")
    got = _find_best_agent_md(str(tmp_path))
    assert got is not None and "42" in got
    assert "Work ledger" not in got


def test_agent_written_markdown_is_still_preferred_by_size(tmp_path):
    """The original behaviour must survive: the largest *agent* file still wins, and
    `output.md` is not excluded — excluding it once discarded exactly the report we
    wanted (see the docstring on the scanner)."""
    _write(tmp_path, "notes.md", "short\n")
    _write(tmp_path, "output.md", "# The actual report\n" + ("detail\n" * 100))
    got = _find_best_agent_md(str(tmp_path))
    assert got is not None and "The actual report" in got


def test_an_explicit_exclude_still_applies(tmp_path):
    """The `exclude` parameter keeps working alongside the ledger filter."""
    _write(tmp_path, "output.md", "# skip me\n" + ("x\n" * 50))
    _write(tmp_path, "real.md", "# keep me\n")
    got = _find_best_agent_md(str(tmp_path), exclude="output.md")
    assert got is not None and "keep me" in got
