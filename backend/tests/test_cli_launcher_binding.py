"""
test_cli_launcher_binding.py — P-0083 item 1: agy's write root is bound to the
session workspace by an explicit launch flag, and nothing may drop it.

Agy resolves relative writes against its *project*, not process cwd. With no
project flag it picks its persisted default project, whose root is the shared
`~/.gemini/antigravity-cli/scratch` — the R4/R5 escape. `--new-project` roots the
project at cwd, and cwd is the session workspace.

The flag is therefore a containment boundary, not a tuning knob: these tests fail
if a future edit makes it conditional on model, budget, tool policy, or version.
"""
from __future__ import annotations

from app.providers.cli_executor import _build_cmd


def _agy(**kw) -> list[str]:
    return _build_cmd("agy", "do the thing", tools_enabled=True, max_rounds=10, **kw)


class TestAgyWorkspaceBinding:
    def test_agy_launch_binds_the_project_to_cwd(self):
        assert "--new-project" in _agy()

    def test_binding_survives_every_optional_flag_combination(self):
        """No model / budget / tool-policy path may drop the binding."""
        for kw in (
            {},
            {"model": "gemini-3-pro"},
            {"budget_usd": 5.0},
            {"model": "gemini-3-pro", "budget_usd": 5.0},
        ):
            cmd = _build_cmd("agy", "p", tools_enabled=True, max_rounds=10, **kw)
            assert "--new-project" in cmd, kw
        # tools disabled means no auto-approve, but containment is not optional
        assert "--new-project" in _build_cmd(
            "agy", "p", tools_enabled=False, max_rounds=10
        )

    def test_binding_is_not_a_project_id(self):
        """`--project` takes an ID agy itself registered; an ID pre-seeded into
        `cache/projects.json` by the control plane is silently ignored in favour of
        the default project (probed live on 1.1.7). Passing one would look like a
        binding and not be one."""
        assert "--project" not in _agy()

    def test_other_clis_do_not_get_the_agy_flag(self):
        for binary in ("claude", "grok", "codex"):
            cmd = _build_cmd(binary, "p", tools_enabled=True, max_rounds=10)
            assert "--new-project" not in cmd, binary
