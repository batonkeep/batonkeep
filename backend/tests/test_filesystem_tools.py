"""
test_filesystem_tools.py — the workdir-scoped read/list/glob/grep tools (P-0046).

Locks the contract: every tool is reachable through the registry, scoped to the
session workdir, rejects path-escape, and grep works through both the ripgrep and
the pure-Python fallback path.
"""
from __future__ import annotations

import json

import pytest

from app.providers.tools.filesystem import FilesystemToolProvider
from app.providers.tools.registry import get_tool_registry

FS_NAMES = {"fs_read", "fs_list", "fs_glob", "fs_grep"}


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "a.py").write_text("import os\nx = 1\nprint(x)\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("hello world\nneedle here\n")
    return tmp_path


def test_provider_registered_in_default_registry():
    names = {t.name for t in get_tool_registry().list_tools()}
    assert FS_NAMES <= names


async def test_fs_read_numbers_lines(workdir):
    out = await FilesystemToolProvider().call_tool(
        "fs_read", {"path": "a.py"}, workdir=str(workdir)
    )
    assert "1\timport os" in out
    assert "3\tprint(x)" in out


async def test_fs_read_offset_limit(workdir):
    out = await FilesystemToolProvider().call_tool(
        "fs_read", {"path": "a.py", "offset": 2, "limit": 1}, workdir=str(workdir)
    )
    assert "x = 1" in out
    assert "import os" not in out


async def test_fs_list_marks_dirs(workdir):
    out = await FilesystemToolProvider().call_tool(
        "fs_list", {"path": "."}, workdir=str(workdir)
    )
    assert "a.py" in out
    assert "sub/" in out


async def test_fs_glob_recursive(workdir):
    out = await FilesystemToolProvider().call_tool(
        "fs_glob", {"pattern": "**/*.txt"}, workdir=str(workdir)
    )
    assert "sub/b.txt" in out
    assert "a.py" not in out


async def test_fs_grep_finds_match(workdir):
    out = await FilesystemToolProvider().call_tool(
        "fs_grep", {"pattern": "needle"}, workdir=str(workdir)
    )
    assert "b.txt" in out
    assert "needle here" in out


async def test_fs_grep_python_fallback(workdir, monkeypatch):
    import app.providers.tools.filesystem as fsmod

    monkeypatch.setattr(fsmod.shutil, "which", lambda _name: None)
    out = await FilesystemToolProvider().call_tool(
        "fs_grep", {"pattern": "needle"}, workdir=str(workdir)
    )
    assert "b.txt:2:needle here" in out


@pytest.mark.parametrize("tool,args", [
    ("fs_read", {"path": "../secret.txt"}),
    ("fs_list", {"path": ".."}),
    ("fs_grep", {"pattern": "x", "path": "../.."}),
])
async def test_path_escape_rejected(workdir, tool, args):
    out = await FilesystemToolProvider().call_tool(tool, args, workdir=str(workdir))
    assert "escapes the working directory" in out


async def test_dispatch_through_registry(workdir):
    out = await get_tool_registry().call(
        "fs_read", json.dumps({"path": "a.py"}), workdir=str(workdir)
    )
    assert "import os" in out
