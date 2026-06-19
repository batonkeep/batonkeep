# Changelog

All notable changes to batonkeep are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
add features freely; patch versions are fixes).

## [0.3.0] — 2026-06-19

A feature release: portable backups for self-hosted installs, and a more reliable
provider-usage display.

### Added

- **`batonkeep-backup` / `batonkeep-restore` scripts for portable backups.** A
  backup script baked into the backend archives only your durable state — the
  database (including encrypted API keys), session workspaces, task outputs, and
  published bundles — while excluding regenerable bloat (`node_modules`,
  virtualenvs, package caches, build output). The result is a small, inspectable
  `.tar.gz` you can stream straight to your host, use to move Batonkeep to new
  hardware, or keep as a snapshot. Restore pipes it back in. See
  [self-hosting → Data & backups](docs/self-hosting.md). Provider OAuth logins are
  deliberately excluded (re-auth after restore); keep your `APP_SECRET` to decrypt
  stored API keys.

### Changed

- **Provider usage display now leans on data we can measure reliably.** The
  background scrape of each plan-CLI's `/usage` panel has been removed — provider
  terminal formats change without notice, which made the quota percentage
  unreliable (and outright broken for some providers). API-key providers now show
  exact spend from recorded run costs; plan-billed providers show a "Plan-billed"
  label with a **Check usage** button that either runs the provider's own usage
  command once and shows the raw output, or drops you into the terminal to read it
  live.

## [0.2.2] — 2026-06-17

A patch release fixing Cloudflare Pages publishing for bundled (built) sites.

### Fixed

- **Cloudflare Pages deploys served an un-built site and rendered blank.** For
  projects with a build step (Vite and similar), the deploy shipped the source
  tree — whose `index.html` points at a dev entry point a browser can't run —
  instead of the build output in `dist/`. Cloudflare publishing now ships the
  built site, matching the in-app share link and download (which were already
  correct). Re-deploy an affected site once on this version to pick up the fix.

## [0.2.1] — 2026-06-17

A maintenance release: a critical fix for agent sessions that install dependencies,
file-browser improvements, chat quality-of-life, and dependency/security updates.

### Fixed

- **Agent sessions could become unresponsive after installing dependencies.** The
  per-session context embedded a full listing of every workspace file; after an
  `npm install` (tens of thousands of files) this could exceed the operating
  system's command-line length limit and break the agent launch ("Argument list
  too long"). Agents are now pointed at the workspace's git repository to discover
  files on demand, so context size no longer grows with the workspace. The
  per-session activity log is likewise capped to a recent tail.
- **File browser flattened nested folders.** Built sites (e.g. `dist/assets/…`)
  now render as a proper nested tree instead of a single flat level. Dependency
  and cache directories (`node_modules`, virtualenvs, `__pycache__`, …) are hidden
  while build output (`dist/`, `build/`) stays visible.

### Added

- **Copy buttons on chat messages** — copy your prompt or the agent's response
  with one click.
- **Live progress while the agent works** — the "Generating…" indicator now
  surfaces the agent's latest step instead of a bare spinner.
- **Routing-decision capture** — batonkeep now records how each turn was routed
  and the outcome, laying groundwork for routing insight and tuning. Adds new
  database tables (see upgrade notes).

### Security

- Dependency updates addressing advisories in Starlette / FastAPI, cryptography,
  python-multipart, pytest, and Vite.

### Upgrade notes

- This release adds database migrations (routing-decision tables). Self-hosted
  deployments apply them automatically on start (Alembic `upgrade head`); no manual
  step is required. Back up your database before upgrading, as always.

## [0.2.0] — 2026-06-16

The API-key lane grows up. Where v0.1.x leaned on the plan-CLI lane for the rich agentic
experience, this release brings the API path to **near-parity** — real tools, code
execution, web search, and full multimodal — so BYO-key and self-hosted models are now a
first-class way to run batonkeep.

### Added

- **Agent tools on the API lane.** API-key providers now run a real tool loop:
  - **Web search** via a self-hosted, key-free **SearXNG** backend, with a transparent
    DuckDuckGo fallback when SearXNG isn't configured.
  - **Filesystem** read / list / glob / grep over the session workspace.
  - **Code execution** — run Python in a pinned, sandboxed environment to produce charts,
    PDFs, CSVs, and scraped data. Gated by a per-session/per-task **execution policy**
    (off / allow-safe / confirm / auto) with an in-UI **approval round-trip** for
    confirm mode.
  - **External tools via MCP** — a curated, SSRF-fenced `fetch` server ships built-in;
    the seam is ready for more.
- **Multimodal.**
  - **Image generation** on the API path (OpenAI `gpt-image-*` and xAI Grok image models),
    capability-gated and budget-metered, saved as normal workspace artifacts.
  - **Image input (vision)** — images you reference in a prompt are passed to
    vision-capable API models (Claude, GPT-4o, Gemini, Grok).
  - **Per-session and per-task image-model selection**, including cross-provider (run text
    on one provider, render images on another).
- **Structured API model catalog** — manage which models are enabled, their pricing, and a
  preferred model per capability, from a Settings editor backed by an overlay file. Plus
  **per-session model selection**.
- **Prompt caching & budgets** — cache-aware cost accounting with caching breakpoints on the
  API loop, and **per-session budget** controls with a live cost chip.
- **Task-run generated assets** — scheduled task runs now **capture, serve, and retain**
  images and data files an agent produces, surfaced in a new **Assets** tab, with per-task
  retention and storage controls.
- **Provider suspend / reactivate** toggle, with suspended providers hidden from task and
  session pickers.

### Changed

- **CLI and API lanes are now near-parity** on tools and multimodal; documentation reframed
  accordingly (the remaining differences are the vendor's full native toolset and per-lane
  failover behavior).
- API tool-result history is compacted to control token cost on long loops.
- UI polish — a **Runs** tab, a relocated session cost/budget chip, and humanized token
  counts.

### Fixed

- The sandbox now **fails closed** when sandboxing is required, rather than silently running
  unsandboxed.
- Several build-session permission and `umask` defects that broke code execution in shared
  workspaces.
- Build-session cost is metered and conversational context preserved across turns.
- A pipe deadlock that could strand agent runs while reading large files.
- SearXNG default engine set trimmed (dropped Tor-only engines that produced log noise).

### Security

- **SSRF egress fence** for the curated `fetch` MCP server.
- Sandbox isolation hardening on the code-execution path.

## [0.1.1] — 2026-06-12

First public release.

[0.2.0]: https://github.com/batonkeep/batonkeep/releases/tag/v0.2.0
[0.1.1]: https://github.com/batonkeep/batonkeep/releases/tag/v0.1.1
