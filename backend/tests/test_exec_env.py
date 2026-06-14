"""
test_exec_env.py — the exec-env manifest + drift-guard (P-0046, Option C).

Locks the manifest contract (the guaranteed code-exec toolchain), the
system-prompt rendering, and the boot drift-guard's skip/detect behaviour.
"""
from __future__ import annotations

import app.exec_env as exec_env
from app.exec_env import ExecEnv, load, render_capabilities, verify

# The founder-directed guaranteed set (P-0046 item 4). Locked so a silent drop
# from the manifest fails the suite.
REQUIRED_LIBS = {
    "httpx", "pandas", "numpy", "pypdf", "python-docx",
    "openpyxl", "beautifulsoup4", "lxml", "matplotlib",
}


def test_manifest_loads_and_declares_guaranteed_set():
    env = load()
    assert REQUIRED_LIBS <= set(env.guaranteed)
    assert env.venv_dir.endswith("/.venv")
    assert env.python_version == "3.12"
    assert {"python3.12", "node", "rg", "git"} <= set(env.system)


def test_pip_specs_are_pinned():
    for spec in load().pip_specs():
        assert "==" in spec


def test_import_name_mapping():
    env = load()
    assert env.import_module_for("python-docx") == "docx"
    assert env.import_module_for("beautifulsoup4") == "bs4"
    # default: normalise dashes to underscores
    assert env.import_module_for("some-pkg") == "some_pkg"


def test_python_bin_under_venv():
    env = load()
    assert env.python_bin == f"{env.venv_dir}/bin/python"


def test_render_capabilities_lists_libs_and_tools():
    blurb = render_capabilities()
    for lib in REQUIRED_LIBS:
        assert lib in blurb
    assert "pip" in blurb
    assert "node" in blurb


def test_verify_skips_when_venv_absent(tmp_path):
    env = ExecEnv(
        venv_dir=str(tmp_path / "nope" / ".venv"),
        python_version="3.12",
        guaranteed={"pandas": "2.2.3"},
        import_names={},
        system=[],
    )
    assert verify(env) == []  # venv missing → skipped, no error


def test_verify_detects_missing(monkeypatch, tmp_path):
    # Pretend the venv exists; stub the import probe to report two modules missing.
    env = ExecEnv(
        venv_dir=str(tmp_path),
        python_version="3.12",
        guaranteed={"pandas": "2.2.3", "python-docx": "1.1.2", "numpy": "2.2.1"},
        import_names={"python-docx": "docx"},
        system=[],
    )
    monkeypatch.setattr(exec_env.os.path, "exists", lambda _p: True)

    class _Result:
        stdout = "docx\nnumpy\n"

    monkeypatch.setattr(exec_env.subprocess, "run", lambda *a, **k: _Result())
    missing = verify(env)
    assert missing == ["numpy", "python-docx"]  # mapped back to dist names, sorted


def test_install_script_reads_same_manifest():
    # The build-time installer parses the manifest directly; confirm its package
    # specs match the runtime loader's — the SSOT is one file, no second list.
    import tomllib

    import scripts.install_exec_env as installer

    with open(installer.MANIFEST, "rb") as f:
        data = tomllib.load(f)
    specs = [f"{n}=={v}" for n, v in data["python"]["guaranteed"].items()]
    assert specs == load().pip_specs()
