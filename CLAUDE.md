# CLAUDE.md — batonkeep (OSS backend engine)

> This is the **`[OSS]` repo** containing the per-user backend engine, single-backend frontend PWA, and enrollment installer. Read this file plus `AGENTS.md` before starting any session. The full spec and all skills live in `batonkeep-ops/`.

## Knowledgebase location

All constitution, milestone specs, and skills are in **`/Users/sks/projects/batonkeep-ops/`**:
- `AGENTS.md` — non-negotiable agent constitution (read first)
- `PLAN.md` — full milestone spec M1–M6 + core
- `skills/` — operational skills (pull in by name as needed)

## Scope of this repo

**This repo is `[OSS]` only.** It must never import or reference `batonkeep-cloud`. A CI check enforces this — do not bypass it.

Modules that belong here: core engine, executor, router, scheduler, API + WS, frontend PWA, `sessions/`, `workspace/`, `preview/`, `publish/`, `artifacts/`, `recipes/`, `library/`, `skills/` (ingest/sandbox/testbench/sync), `pipelines/`, `actions/`, `audit/`, enrollment installer client, `register`/`heartbeat` endpoints.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 async + aiosqlite · APScheduler · `openai` + `anthropic` SDKs · WebSocket · React + Vite + TS + Tailwind (PWA) · Docker Compose.

## Quick rules (full constitution in `batonkeep-ops/AGENTS.md`)

1. Phase-gated: run the Verify gate before advancing; commit per phase.
2. Mock first — real providers only at the designated phase.
3. Never touch subscription OAuth tokens.
4. Never hardcode secrets.
5. Every owned row is `owner_id`-scoped.
6. `DEPLOYMENT_MODE=managed` refuses plan-CLI mode at config load — CI-asserted.
7. Async throughout. Sandbox untrusted code (M1/M3).

## Design system (frontend)

"Mission-control / terminal control-room." Not generic AI look.
- **Palette:** near-black `#0a0b0d`; warm off-white text; amber `#f5b700` accent; cyan for live/streaming only.
- **Type:** headings **IBM Plex Mono**; body **IBM Plex Sans**; metrics in mono.
- **Texture:** faint blueprint grid + subtle grain; staggered CSS load-in.
- **Mobile-first PWA** — primary interface is a phone.
