"""sessions/ — build-session engine (M1.1).

A build session is an interactive, multi-turn conversation with a chosen CLI-plan
agent against a sandboxed, git-init'd per-session workspace. The workspace
filesystem is the source of truth (D-0008), which is what makes the mid-session
multi-agent switch clean: a new executor continues from the workspace + SESSION.md
brief, not a replayed transcript.
"""
