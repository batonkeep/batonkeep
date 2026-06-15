"""
providers/tools/file_write.py — write a file to the task workdir.
"""
from __future__ import annotations

import os

from app.sessions.workspace import group_writable

TOOL_SCHEMA = {
    "name": "file_write",
    "description": "Write content to a file in the task's working directory.",
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Relative filename within the workdir."},
            "content": {"type": "string", "description": "File content to write."},
        },
        "required": ["filename", "content"],
    },
}

# Injected by model_executor before calling this tool
_WORKDIR: str = "/tmp"


async def run(filename: str, content: str, *, workdir: str = "/tmp") -> str:
    # Sanitise: only relative paths, no ..
    safe = os.path.normpath(filename).lstrip("/")
    if ".." in safe:
        return "[file_write error] path traversal rejected"
    target = os.path.join(workdir, safe)
    # Group-write umask so agent-authored files land co-writable by the
    # sandbox-user agent (build/code_exec) as well as batond — the shared session
    # tree is setgid `agents` (P-0022/D-0020). Without this, default-umask 0644
    # files are group read-only and the sandbox lane hits EACCES editing them.
    # No await inside the block (umask is process-global — see group_writable).
    with group_writable():
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    return f"[file_write] wrote {len(content)} chars to {target}"
