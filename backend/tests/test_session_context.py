"""
test_session_context.py — terminal-lane filesystem-as-context seeding (D-0017).

Verifies the managed-block write into a provider's convention file: correct file
per provider, brief + files rendered, non-destructive to user content, idempotent
refresh, and that the convention files are excluded from publish.
"""
from __future__ import annotations

import os

from app.sessions import session_context as sc
from app.sessions.workspace import BRIEF_FILENAME


def _ws(tmp_path, brief="# Build a landing page\n\nGoal: a catering site.", files=("index.html", "style.css")):
    d = tmp_path / "ws"
    d.mkdir()
    if brief is not None:
        (d / BRIEF_FILENAME).write_text(brief, encoding="utf-8")
    for f in files:
        (d / f).write_text("x", encoding="utf-8")
    return str(d)


def test_convention_file_per_provider():
    assert sc.context_filename("claude") == "CLAUDE.md"
    # agy (Antigravity) auto-loads AGENTS.md in preference to GEMINI.md when both
    # exist, so we unify on AGENTS.md to stay authoritative across provider switches.
    assert sc.context_filename("agy") == "AGENTS.md"
    assert sc.context_filename("codex") == "AGENTS.md"
    assert sc.context_filename("unknown") == "AGENTS.md"  # default


def test_seed_writes_brief_and_files(tmp_path):
    ws = _ws(tmp_path)
    fname = sc.seed_provider_context(ws, "claude")
    assert fname == "CLAUDE.md"
    text = (tmp_path / "ws" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Batonkeep session context" in text
    assert "Build a landing page" in text   # brief content
    assert "- index.html" in text and "- style.css" in text  # file list
    assert sc._BEGIN in text and sc._END in text


def test_seed_excludes_brief_and_convention_files_from_listing(tmp_path):
    ws = _ws(tmp_path, files=("index.html",))
    sc.seed_provider_context(ws, "claude")  # creates CLAUDE.md
    sc.seed_provider_context(ws, "codex")   # creates AGENTS.md
    text = (tmp_path / "ws" / "AGENTS.md").read_text(encoding="utf-8")
    # the listing must not include SESSION.md, CLAUDE.md, or AGENTS.md
    assert "- index.html" in text
    for noise in (BRIEF_FILENAME, "CLAUDE.md", "AGENTS.md"):
        assert f"- {noise}" not in text


def test_seed_is_non_destructive_to_user_content(tmp_path):
    ws = _ws(tmp_path)
    user_md = "# My project rules\n\nAlways use tabs.\n"
    (tmp_path / "ws" / "CLAUDE.md").write_text(user_md, encoding="utf-8")
    sc.seed_provider_context(ws, "claude")
    text = (tmp_path / "ws" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Always use tabs." in text          # user content preserved
    assert "Batonkeep session context" in text  # our block added


def test_seed_refresh_replaces_only_managed_block(tmp_path):
    ws = _ws(tmp_path)
    user_md = "# rules\nkeep me\n"
    (tmp_path / "ws" / "CLAUDE.md").write_text(user_md, encoding="utf-8")
    sc.seed_provider_context(ws, "claude")
    # add a new file, refresh — block updates, user content still there, single block
    (tmp_path / "ws" / "new.js").write_text("y", encoding="utf-8")
    sc.seed_provider_context(ws, "claude")
    text = (tmp_path / "ws" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "keep me" in text
    assert text.count(sc._BEGIN) == 1 and text.count(sc._END) == 1  # not duplicated
    assert "- new.js" in text


def test_convention_files_excluded_from_publish():
    from app.sessions.publish import _EXCLUDED_TOP
    assert {"CLAUDE.md", "AGENTS.md", "GEMINI.md"} <= _EXCLUDED_TOP
