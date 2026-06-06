"""
test_exec_seam.py — execution-seam posture (D-0016).

The user-facing "exec-seam = terminal" override (D-0015 slice 3) is removed: task
turns ALWAYS run headless `cli -p`, and the full-TTY interactive driver survives
only as an automated-internal single-shot helper (get_interactive_executor, used
by /usage capture). Users get headless (tasks) or web-TTY (live), never autonomous
TUI driving as a task lane.
"""
from __future__ import annotations

import pytest

from app.providers import registry


class TestTaskExecutorIsAlwaysHeadless:
    def test_cli_returns_headless_executor(self):
        ex = registry.get_executor("claude")
        if ex is None:
            pytest.skip("claude instance unavailable in this deployment mode")
        from app.providers.cli_executor import CLIExecutor
        assert isinstance(ex, CLIExecutor)

    def test_no_user_facing_seam_toggle(self):
        # The override surface is gone — task routing can't select the TUI driver.
        assert not hasattr(registry, "set_exec_seam")
        assert not hasattr(registry, "get_exec_seam")


class TestInteractiveExecutorIsInternalOnly:
    def test_interactive_helper_returns_interactive_executor(self):
        ex = registry.get_interactive_executor("claude")
        if ex is None:
            pytest.skip("claude instance unavailable in this deployment mode")
        from app.providers.cli_interactive import CLIInteractiveExecutor
        assert isinstance(ex, CLIInteractiveExecutor)

    def test_interactive_helper_rejects_non_cli(self):
        # mock is not a CLI → no interactive driver.
        assert registry.get_interactive_executor("mock") is None

    def test_interactive_helper_unknown_instance(self):
        assert registry.get_interactive_executor("nope:404") is None
