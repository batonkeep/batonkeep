"""
providers/tools/filesystem.py — workdir-scoped read / list / glob / grep tools.

P-0046 workstream 1 (Tier A), V1. The API path (`ModelExecutor`) had **no** way
to read, list, or search files — `file_write` is write-only — while the CLI lane
leans on Read/Glob/Grep as its hardest-used navigation primitives. This closes
that gap with a curated first-party `ToolProvider`: no new trust surface beyond
today's built-ins (same session sandbox, same vetted-code posture), so it is
unblocked now and does not need the P-0012 trust model.

Every tool is **scoped to the session workdir**. Paths are resolved against the
workdir and rejected if they escape it (symlinks included, via realpath). Grep is
ripgrep-backed for speed and correctness (the official filesystem MCP server's
content search is weak — P-0046), with a pure-Python regex fallback when `rg` is
not on PATH so the tool degrades gracefully rather than disappearing.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil

from app.providers.tools.registry import McpTool, ToolProvider

# Caps — keep tool output bounded so a single call can't blow the context window.
_MAX_READ_BYTES = 256 * 1024
_MAX_READ_LINES = 2000
_MAX_LIST_ENTRIES = 1000
_MAX_GLOB_MATCHES = 500
_MAX_GREP_MATCHES = 500
_GREP_TIMEOUT_S = 20.0

FS_READ_SCHEMA = {
    "name": "fs_read",
    "description": (
        "Read a text file from the working directory. Returns the file content "
        "with 1-based line numbers. Use offset/limit to page through large files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path within the workdir."},
            "offset": {"type": "integer", "description": "1-based line to start from (default 1)."},
            "limit": {
                "type": "integer",
                "description": f"Max lines to read (default {_MAX_READ_LINES}).",
            },
        },
        "required": ["path"],
    },
}

FS_LIST_SCHEMA = {
    "name": "fs_list",
    "description": "List the entries of a directory in the working directory (non-recursive).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative directory path (default '.')."},
        },
    },
}

FS_GLOB_SCHEMA = {
    "name": "fs_glob",
    "description": (
        "Find files in the working directory matching a glob pattern "
        "(e.g. '**/*.py', 'src/*.ts'). Returns matching relative paths."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, '**' matches any depth."},
        },
        "required": ["pattern"],
    },
}

FS_GREP_SCHEMA = {
    "name": "fs_grep",
    "description": (
        "Search file contents in the working directory for a regular expression "
        "(ripgrep-backed). Returns matching lines as 'path:line:text'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": "string",
                "description": "Relative subdir/file to scope the search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Optional glob to filter files, e.g. '*.py'.",
            },
        },
        "required": ["pattern"],
    },
}


def _resolve(workdir: str, rel: str) -> str | None:
    """Resolve `rel` against `workdir`, returning an absolute path only if it
    stays inside the workdir (symlinks resolved). Returns None on escape."""
    base = os.path.realpath(workdir)
    target = os.path.realpath(os.path.join(base, rel or "."))
    if target == base or target.startswith(base + os.sep):
        return target
    return None


class FilesystemToolProvider(ToolProvider):
    """Curated, workdir-scoped filesystem read/navigation tools (P-0046 Tier A)."""

    _SCHEMAS = {
        "fs_read": FS_READ_SCHEMA,
        "fs_list": FS_LIST_SCHEMA,
        "fs_glob": FS_GLOB_SCHEMA,
        "fs_grep": FS_GREP_SCHEMA,
    }

    def list_tools(self) -> list[McpTool]:
        return [
            McpTool(name=s["name"], description=s["description"], input_schema=s["parameters"])
            for s in self._SCHEMAS.values()
        ]

    async def call_tool(
        self, name: str, arguments: dict, *, workdir: str, context: dict | None = None
    ) -> str:
        if name == "fs_read":
            return self._read(workdir, **arguments)
        if name == "fs_list":
            return self._list(workdir, **arguments)
        if name == "fs_glob":
            return self._glob(workdir, **arguments)
        if name == "fs_grep":
            return await self._grep(workdir, **arguments)
        return f"[unknown tool: {name}]"

    # ── fs_read ────────────────────────────────────────────────────────────────
    def _read(self, workdir: str, path: str, offset: int = 1, limit: int = _MAX_READ_LINES) -> str:
        target = _resolve(workdir, path)
        if target is None:
            return "[fs_read error] path escapes the working directory"
        if not os.path.isfile(target):
            return f"[fs_read error] not a file: {path}"
        if os.path.getsize(target) > _MAX_READ_BYTES:
            return (
                f"[fs_read error] file too large (> {_MAX_READ_BYTES} bytes); "
                "use offset/limit or fs_grep"
            )
        offset = max(1, int(offset))
        limit = max(1, min(int(limit), _MAX_READ_LINES))
        with open(target, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        chunk = lines[offset - 1: offset - 1 + limit]
        if not chunk:
            return f"[fs_read] {path}: no lines at offset {offset} (file has {len(lines)} lines)"
        out = [f"{offset + i:>6}\t{ln.rstrip(chr(10))}" for i, ln in enumerate(chunk)]
        return "\n".join(out)

    # ── fs_list ──────────────────────────────────────────────────────────────────
    def _list(self, workdir: str, path: str = ".") -> str:
        target = _resolve(workdir, path)
        if target is None:
            return "[fs_list error] path escapes the working directory"
        if not os.path.isdir(target):
            return f"[fs_list error] not a directory: {path}"
        entries = sorted(os.listdir(target))[:_MAX_LIST_ENTRIES]
        if not entries:
            return f"[fs_list] {path}: (empty)"
        rows = []
        for name in entries:
            full = os.path.join(target, name)
            rows.append(f"{name}/" if os.path.isdir(full) else name)
        return "\n".join(rows)

    # ── fs_glob ──────────────────────────────────────────────────────────────────
    def _glob(self, workdir: str, pattern: str) -> str:
        base = _resolve(workdir, ".")
        if base is None:
            return "[fs_glob error] bad working directory"
        matches: list[str] = []
        for root, _dirs, files in os.walk(base):
            for fname in files:
                rel = os.path.relpath(os.path.join(root, fname), base)
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fname, pattern):
                    matches.append(rel)
                    if len(matches) >= _MAX_GLOB_MATCHES:
                        break
            if len(matches) >= _MAX_GLOB_MATCHES:
                break
        if not matches:
            return f"[fs_glob] no files match {pattern!r}"
        return "\n".join(sorted(matches))

    # ── fs_grep ──────────────────────────────────────────────────────────────────
    async def _grep(
        self, workdir: str, pattern: str, path: str = ".", glob: str | None = None
    ) -> str:
        scope = _resolve(workdir, path)
        if scope is None:
            return "[fs_grep error] path escapes the working directory"
        rg = shutil.which("rg")
        if rg:
            return await self._grep_rg(rg, workdir, scope, pattern, glob)
        return self._grep_py(workdir, scope, pattern, glob)

    async def _grep_rg(
        self, rg: str, workdir: str, scope: str, pattern: str, glob: str | None
    ) -> str:
        args = [
            rg, "--no-heading", "--line-number", "--color", "never",
            "-m", str(_MAX_GREP_MATCHES),
        ]
        if glob:
            args += ["--glob", glob]
        args += ["--", pattern, scope]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GREP_TIMEOUT_S)
        except TimeoutError:
            return "[fs_grep error] search timed out"
        if proc.returncode not in (0, 1):  # 1 = no matches, not an error
            return f"[fs_grep error] {stderr.decode('utf-8', 'replace').strip()}"
        base = os.path.realpath(workdir)
        lines = []
        for raw in stdout.decode("utf-8", "replace").splitlines():
            # ripgrep emits absolute paths (we passed an absolute scope); relativise.
            parts = raw.split(":", 2)
            if len(parts) == 3 and os.path.isabs(parts[0]):
                parts[0] = os.path.relpath(parts[0], base)
                raw = ":".join(parts)
            lines.append(raw)
        if not lines:
            return f"[fs_grep] no matches for {pattern!r}"
        return "\n".join(lines[:_MAX_GREP_MATCHES])

    def _grep_py(self, workdir: str, scope: str, pattern: str, glob: str | None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"[fs_grep error] bad regex: {exc}"
        base = os.path.realpath(workdir)
        targets: list[str] = []
        if os.path.isfile(scope):
            targets = [scope]
        else:
            for root, _dirs, files in os.walk(scope):
                for fname in files:
                    if glob and not fnmatch.fnmatch(fname, glob):
                        continue
                    targets.append(os.path.join(root, fname))
        out: list[str] = []
        for fpath in targets:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if rx.search(line):
                            rel = os.path.relpath(fpath, base)
                            out.append(f"{rel}:{n}:{line.rstrip(chr(10))}")
                            if len(out) >= _MAX_GREP_MATCHES:
                                return "\n".join(out)
            except (OSError, ValueError):
                continue
        if not out:
            return f"[fs_grep] no matches for {pattern!r}"
        return "\n".join(out)
