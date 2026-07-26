# Changelog

All notable changes to batonkeep are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
add features freely; patch versions are fixes).

## [0.7.0] — 2026-07-26

The largest release so far. batonkeep gains **Projects** — durable containers for
long-running work, with their own context, work items, approvals and evidence — so
a build session is no longer a thing that happens once and scrolls away. Alongside
it: each agent is now confined to its own workspace, and a turn that reports
success has to prove it.

Upgrading from 0.6.0 is a normal `make pull && make up`. The database migrates
automatically; every existing task, session and run is attached to a "Personal
workspace" project, so nothing moves and nothing is lost.

### Added

- **Projects.** A project holds the context, work items, approvals and evidence for
  a body of work that outlives a single session. Declare the files that make up its
  canonical context; batonkeep projects that context into each new session, records
  a receipt of exactly what the agent was given, and keeps the results. There's a
  new **Projects** section in the navigation, and build sessions are grouped under
  the project they belong to. See `docs/projects.md`.
- **Work items and output contracts.** Break a project into work items, each with a
  sub-task checklist naming the files it should produce. Progress is measured
  against what actually landed in the workspace — not against what the agent said
  it did.
- **A project planner.** Point a project at a planner model and ask it to read the
  project's own context and propose structure — work items, next actions, gaps. It
  only ever *proposes*: nothing it suggests takes effect until you accept it.
- **Approvals and an evidence store.** Canonical writes go through an approval you
  can review, and approving one now also declares it as project context, so the
  next session actually sees it. Related pending writes can be approved as one
  batch. Approved artifacts, packages and manifests are kept as durable evidence and
  can be pinned to the work item they belong to.
- **Workspace packages as evidence.** Capture a session's workspace as a zip with a
  manifest of every file and its checksum, attributed to the turn that produced it —
  a checkpoint you can hand to a later session or a different provider.
- **Evidence viewer.** Text evidence renders in place instead of downloading.
- **Per-agent workspace isolation.** On Linux hosts with Landlock (kernel 5.13+),
  each agent process is now confined to its own session workspace plus a read-only
  system view, so one build session can no longer read or write another's files.
  `SANDBOX_JAIL` controls the policy: `warn` (default) applies the jail where the
  kernel supports it and warns loudly where it does not; `require` refuses to launch
  an agent that cannot be confined; `off` disables it. **What it covers is
  workspaces** — `/tmp` and the agent home stay shared between sessions, which is
  fine for a single-operator install but means "fully isolated" would overstate it.
  The backend reports the jail's real state at startup *and* on `/health`, so you can
  confirm it rather than assume it — and an image that predates the feature reports no
  jail state at all, which is how you spot that setting the variable did nothing. See
  `docs/self-hosting.md`.

### Changed

- **"Succeeded" now means the work arrived.** A turn that claims to have written
  files is checked against the workspace. If the files aren't there, the turn says
  so — with the specific paths — instead of reporting a clean success. A run that
  produced no output at all, or that hit its timeout, is classified as such rather
  than lumped in with real failures.
- **Cancelling a turn no longer discards what it finished.** Stopping mid-flight
  snapshots the workspace as it stands, so completed work survives the cancellation
  instead of being lost with the turn.
- **Build sessions are grouped by project** in the sidebar, collapsed by default with
  counts, and the `@` context picker offers the project's declared sources live.

### Fixed

- **Agents can no longer write their output outside your session.** One CLI agent
  resolved file writes against its own persisted project directory rather than the
  workspace it was given, so a build could report three files written and leave the
  session empty. Each headless run is now bound to its session's workspace, and a
  turn whose claimed outputs are missing is flagged rather than versioned and
  packaged as if it had delivered.
- **An agent could take ownership of its workspace's git repository**, after which
  batonkeep could no longer read that session's history and said nothing about it —
  it simply looked like a session with no commits. Ownership is now enforced, the
  condition is reported instead of swallowed, and a repair path re-adopts workspaces
  already affected.
- **Build environments no longer flood the file diff.** A `.venv` or `node_modules`
  the agent created could turn three real outputs into hundreds of changed-file rows.
  Those trees are excluded at the commit boundary; the fact an environment was built
  is still noted on the turn.
- **The frontend container no longer reports itself unhealthy while serving fine.**
  The health probe resolved to IPv6 while nginx listened on IPv4, producing a
  permanent `unhealthy` state alongside perfectly good responses.
- **A newer database than the code understands now fails closed** at startup with a
  clear message, instead of running against a schema it cannot interpret — which
  matters if you ever roll an install back to an older image.

## [0.6.0] — 2026-07-14

A security-and-polish release: add a second factor to your login, and a cleaner
build view on mobile.

### Added

- **Two-factor authentication (TOTP).** When you protect batonkeep with a login
  password (`APP_PASSWORD`), you can now add an authenticator-app second factor
  from **Settings → Security**. Scan the QR code (or type the key) into Google
  Authenticator, Aegis, 1Password, or any TOTP app, confirm once, and login then
  asks for a 6-digit code alongside your password. It's entirely optional and off
  until you enrol. Lost your device? Set `TOTP_DISABLED=1` in your deployment
  environment to sign in with the password alone, then re-enrol.

### Changed

- **Tighter cross-origin defaults.** A new `CORS_ALLOW_ORIGINS` setting lets you
  pin the API to your own UI origin(s) instead of reflecting any origin — worth
  setting if you don't run batonkeep behind its own login gate.
- **Safer rendering of model output.** Markdown produced during a build session
  is now sanitised before display, closing an avenue for embedded scripts in
  agent- or file-sourced content to run in the dashboard.

### Fixed

- **Build-session header no longer overflows on phones.** Once a site was built
  and Cloudflare was configured, the action buttons — including Cloudflare and
  Publish — could run off the right edge on a narrow screen. The header now
  collapses those actions to icons on mobile so every one stays reachable.

## [0.5.1] — 2026-06-21

A fix release: stay logged in.

### Fixed

- **Sessions no longer expire early.** The login session cookie was missing an
  absolute expiry, so some browsers — notably Safari on iOS and macOS — treated
  it as a session-only cookie and dropped it well before its 14-day lifetime,
  forcing a re-login after a few hours on desktop or roughly daily on mobile. The
  cookie now carries an explicit expiry and persists for the full session
  lifetime across all browsers.

### Added

- **`COOKIE_SECURE` setting.** When you reach batonkeep over TLS — a cloudflared
  tunnel or a TLS-terminating reverse proxy — set `COOKIE_SECURE=true` so the
  login session cookie is only ever sent over HTTPS. Leave it unset for plain-http
  LAN access, where a `Secure` cookie would never be sent and login would fail.
- **Live-feed keepalive.** The live activity WebSocket now sends a periodic
  heartbeat so it isn't dropped by an upstream proxy's idle timeout (e.g.
  Cloudflare's ~100s limit) during quiet periods — which previously made the
  dashboard briefly show "offline" when left idle.

## [0.5.0] — 2026-06-20

A small release that makes your install's version visible.

### Added

- **Version display.** batonkeep now shows which version you're running — in
  Settings → About and, on desktop, at the bottom of the left navigation rail —
  with a link to the matching release notes on GitHub. When a newer release is
  available, an unobtrusive indicator appears next to the version; there is no
  pop-up or nag. The update check reads a small static file published on the
  batonkeep website (not the GitHub API), sends no information about your
  instance, and can be disabled by setting `VERSION_CHECK_URL` to an empty value.

## [0.4.0] — 2026-06-19

A feature release centred on build-session control: you can now stop a running
turn mid-flight and keep going, set per-task run timeouts, and trust failed runs
to report honestly.

### Added

- **Stop a running build-session turn.** While a turn is in flight, the Send
  button becomes a **Stop** button. Stopping cancels the underlying CLI run,
  preserves whatever was streamed so far, and marks the turn cancelled without
  committing the workspace. The running state (and the Stop button) now survives
  switching between sessions, and your next message simply continues from the
  workspace and recent dialogue — so you can continue even after switching to a
  different provider.
- **Per-task run timeout.** Tasks have an optional **Run timeout (minutes)** field
  in the form's Advanced section (1–360 min) that overrides the global 30-minute
  default. On expiry the in-flight run is cancelled and fails honestly with a
  timeout error; already-finished deliverables are never lost to a late timeout.
- **Standardised Python dependency workflow for agents.** Session context now
  guides agents to install custom Python packages into a `.venv` at the workspace
  root (via `uv`, falling back to `pip`) and record them in `requirements.txt`.
  Because `.venv` is already excluded from share/download bundles, this keeps
  generated bundles clean and reproducible.

### Fixed

- **Truncated grok runs are now reported as failures.** When the grok CLI hit its
  turn cap or was cancelled mid-research, the run was previously reported as a
  successful "plain-text fallback" from partial output. It now detects the
  truncation and records a real failure, so the orchestrator can fail over instead
  of returning an incomplete result.
- **Stranded turns are cleaned up on restart.** Session turns left running by a
  backend restart are now marked failed at startup instead of sitting non-terminal
  forever (and showing a phantom Stop button).

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
