"""
sessions/templates.py — session task types (P-0010 / D-0011).

The high-frequency entry tasks (summarize a document, draft content) ride the same
build-session engine as website-gen — they are just a different output modality
(Four-Layer model). A template is a preset that seeds the session's goal + a task
guidance block into SESSION.md, which the orchestrator already injects into every
turn's context (filesystem-as-context, D-0008 A). So templates need **no engine
change**: the agent reads the guidance each turn.

Scope (D-0011): ship #1 Summarize, #2 Web research, #3 Draft. The sandbox has
network egress by default (agents require it to function), so web research is not
blocked — it relies on the executor's own web tools. Image/video stay out
(multimodal gap, D-0008 B).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionTemplate:
    id: str
    label: str
    description: str
    # Default session goal (the user can override at/after creation).
    goal: str
    # Task guidance embedded in SESSION.md — read by the agent on every turn.
    guidance: str


# Ordered for display. "blank" is implicit (no template → today's plain session).
_TEMPLATES: list[SessionTemplate] = [
    SessionTemplate(
        id="summarize",
        label="Summarize a document",
        description="Drop a PDF, CSV, or text file and get a clear summary you can refine.",
        goal="Summarize an uploaded document into a clear, useful brief.",
        guidance=(
            "This is a **document-summarization** session.\n"
            "- The user will upload one or more files (PDF/CSV/TXT/MD) into the workspace "
            "(see the file list). Read them from disk.\n"
            "- Produce `summary.md` in the workspace: a tight executive summary, the key "
            "points as bullets, and any notable figures/dates. Cite the source filename.\n"
            "- For CSV, summarize the columns and the headline numbers; for long PDFs, "
            "structure by section.\n"
            "- Keep it factual — do not invent content that isn't in the source. Ask for the "
            "file if none has been uploaded yet."
        ),
    ),
    SessionTemplate(
        id="research",
        label="Research a topic",
        description="Research a question or topic on the web and synthesize the findings.",
        goal="Research a topic and produce a synthesized, sourced brief.",
        guidance=(
            "This is a **web-research** session.\n"
            "- Use your web tools to research the user's question across multiple sources; "
            "the sandbox has network access.\n"
            "- Write the synthesis to `research.md` in the workspace: the answer up top, then "
            "the supporting findings as bullets, each with the **source URL** so claims are "
            "traceable.\n"
            "- Prefer recent, reputable sources; note disagreement between sources rather than "
            "papering over it. Distinguish what you found from what you inferred.\n"
            "- If your executor has no web access, say so plainly instead of inventing sources."
        ),
    ),
    SessionTemplate(
        id="draft",
        label="Draft content",
        description="Write an email, report, or proposal — iterate until it's right.",
        goal="Draft a piece of written content and refine it through the conversation.",
        guidance=(
            "This is a **content-drafting** session.\n"
            "- Write the requested piece (email, report, proposal, post) to a workspace file "
            "(e.g. `draft.md`), so it is versioned and the user can iterate on it.\n"
            "- Match the tone/length the user asks for; if unspecified, default to clear and "
            "concise, and ask one clarifying question only if essential.\n"
            "- If the user uploaded reference material, ground the draft in it.\n"
            "- On each turn, revise the existing file rather than starting over, so Undo/History "
            "captures the progression."
        ),
    ),
]

_BY_ID = {t.id: t for t in _TEMPLATES}


def list_templates() -> list[SessionTemplate]:
    return list(_TEMPLATES)


def get_template(template_id: str) -> SessionTemplate | None:
    return _BY_ID.get(template_id)
