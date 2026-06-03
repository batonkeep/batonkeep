# Batonkeep

**Your plans, your keys, your machine — coordinated.**

Batonkeep is the device-independent control plane for your own subscription-backed AI
agents. Schedule tasks, run interactive build sessions, switch providers mid-task when
rate-limited, and publish work to shareable URLs — all on a backend you control.

> This is the `[OSS]` engine. The proprietary multi-user control plane lives in
> `batonkeep-cloud`. The ops knowledgebase and build specs live in `batonkeep-ops`.

## What it does

- **Task scheduler** — run prompts against your own Claude / Grok / Antigravity / Codex
  plans or API keys on a cron/interval schedule.
- **Capacity routing + failover** — when one plan is rate-limited, work fails over to the
  next. You configure the candidate order; the router handles the rest.
- **Sovereignty** — your subscription credentials never leave the machine running this
  backend. No third-party service holds your tokens.
- **Device-independent PWA** — the frontend is installable; the phone is a first-class client.
- *(Coming: M1)* **Build sessions** — interactive agent sessions with live preview and
  one-click artifact publishing to a shareable URL.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 async + aiosqlite · APScheduler ·
`openai` + `anthropic` SDKs · React + Vite + TypeScript + Tailwind · Docker Compose

## Quickstart

```bash
cp .env.example .env          # set DATABASE_URL, API keys, or leave defaults
make up                       # docker compose up — dashboard at http://localhost:8080
make auth                     # log in to your subscription-plan CLIs (claude, grok, agy)
```

## License

AGPLv3. See `LICENSE`. Enterprise/commercial licensing available — open an issue.

## Contributing

See `CONTRIBUTING.md`. All contributions require a DCO sign-off (`git commit -s`).
