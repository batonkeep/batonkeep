# AGENTS.md — batonkeep (OSS backend engine)

> Always-on context for agents working in this repo. The **full constitution and milestone spec** live in `batonkeep-ops/AGENTS.md` and `batonkeep-ops/PLAN.md` — read those before any build session. This file is a lightweight pointer.

## This repo: `batonkeep` [OSS]

Per-user backend engine · single-backend frontend PWA · enrollment installer (client-side) · public REST/WS API. Milestones M1–M4, M6, core, and gateway.

**Must never import or reference `batonkeep-cloud`.** CI enforces this.

## Full knowledgebase

Located at `/Users/sks/projects/batonkeep-ops/`:

```
AGENTS.md    ← full agent constitution (read this)
PLAN.md      ← full milestone spec M1–M6
skills/
  build-workflow.md
  cli-executor-and-quota.md
  routing-and-failover.md
  sandbox-isolation.md
  open-core-boundary.md
  enrollment-installer.md
```

## Starting a session

1. Read `batonkeep-ops/AGENTS.md` — constitution + skill index.
2. Read `batonkeep-ops/PLAN.md §<milestone>` for your current work.
3. Pull in the relevant skills.
4. Build phase-by-phase per the `build-workflow` skill.
