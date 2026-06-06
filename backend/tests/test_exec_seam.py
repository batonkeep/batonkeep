"""
test_exec_seam.py — D-0015 per-instance exec-seam override (slice 3).

The override store mirrors model-overrides: runtime JSON, default "headless".
get_executor() routes a CLI instance to the PTY seam when set to "terminal".
"""
from __future__ import annotations

import pytest

from app.providers import registry


@pytest.fixture
def seam_store(tmp_path, monkeypatch):
    """Point the override store at a temp file and reset in-memory state."""
    path = tmp_path / "exec-seam.json"
    monkeypatch.setattr(registry, "_EXEC_SEAM_OVERRIDES_PATH", str(path))
    monkeypatch.setattr(registry, "_EXEC_SEAM_OVERRIDES", {})
    return path


class TestSeamOverride:
    def test_default_is_headless(self, seam_store):
        assert registry.get_exec_seam("claude") == "headless"

    def test_set_terminal_persists(self, seam_store):
        registry.set_exec_seam("claude", "terminal")
        assert registry.get_exec_seam("claude") == "terminal"
        assert seam_store.exists()
        assert registry._load_exec_seam_overrides()["claude"] == "terminal"

    def test_reset_to_headless_clears_entry(self, seam_store):
        registry.set_exec_seam("claude", "terminal")
        registry.set_exec_seam("claude", "headless")
        assert registry.get_exec_seam("claude") == "headless"
        assert "claude" not in registry._EXEC_SEAM_OVERRIDES

    def test_invalid_seam_ignored(self, seam_store):
        registry.set_exec_seam("claude", "bogus")
        assert registry.get_exec_seam("claude") == "headless"


class TestGetExecutorRouting:
    def test_headless_returns_cli_executor(self, seam_store):
        ex = registry.get_executor("claude")
        if ex is None:
            pytest.skip("claude instance unavailable in this deployment mode")
        from app.providers.cli_executor import CLIExecutor
        assert isinstance(ex, CLIExecutor)

    def test_terminal_returns_interactive_executor(self, seam_store):
        if registry.get_executor("claude") is None:
            pytest.skip("claude instance unavailable in this deployment mode")
        registry.set_exec_seam("claude", "terminal")
        ex = registry.get_executor("claude")
        from app.providers.cli_interactive import CLIInteractiveExecutor
        assert isinstance(ex, CLIInteractiveExecutor)
