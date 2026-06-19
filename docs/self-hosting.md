# Self-hosting batonkeep

batonkeep runs as two containers from prebuilt images on [GHCR](https://github.com/orgs/batonkeep/packages):
a backend (control plane + agent CLIs) and a frontend (nginx serving the SPA and
reverse-proxying the API/WebSocket). **Only the frontend is exposed** — the backend is
reachable only on the internal compose network. That single web port is the one surface you
put a domain and TLS in front of.

## Host requirements

- **Docker Engine** 24+ recommended (20.10+ is known to work). The Compose **v2 plugin**
  (`docker compose`) is recommended; the legacy standalone **`docker-compose` v1 (1.29.2) also
  works** — it parses this Compose file and honors the healthcheck-gated startup and the
  `selfhost` profile.
- **Architecture:** `amd64` or `arm64` — both are published as a multi-arch manifest, so
  `docker pull` selects the right one automatically (Apple Silicon and ARM VPS included).
- **Memory:** ~1 GB free for the stack; the backend is capped at 768 MB. Build sessions and
  local inference want more.
- **Disk:** a few GB for images plus your data volumes (`appdata`, `agent_home`).
- **Outbound network** to your providers and to GHCR for image pulls.

## Install

```bash
curl -fsSLO https://raw.githubusercontent.com/batonkeep/batonkeep/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/batonkeep/batonkeep/main/.env.example -o .env
curl -fsSLO https://raw.githubusercontent.com/batonkeep/batonkeep/main/searxng-settings.yml
# edit .env — see "Configure your environment" below
docker compose up -d
```

> **Grab all three files.** The compose file bind-mounts `searxng-settings.yml` (next to it)
> to power the built-in **SearXNG** web-search backend, which runs by default. If the file is
> missing, the `searxng` container can't start cleanly — download it alongside the other two.
> (You can run without search by removing the `searxng` service from the compose file;
> `web_search` then falls back to a DuckDuckGo scrape.)

### Configure your environment

Edit the `.env` you just downloaded. Two settings matter before you expose the app:

- **`APP_SECRET`** — *required.* Encrypts stored credentials at rest. Generate one:

  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

- **`APP_PASSWORD`** — *strongly recommended, especially on a public host or VPS.* When set,
  the **entire app requires login** — this protects your **data**, not just the UI. **If you
  leave it empty there is no auth gate and anyone who can reach the web port has full access.**

```bash
# .env
APP_SECRET=<paste the generated 64-hex value>
APP_PASSWORD=<a strong password>      # leave unset only on a trusted private network
```

`DEFAULT_CANDIDATES` ships as `mock` so a fresh install runs with zero credentials; switch it
to your real providers (e.g. `claude,grok`) after the auth step below. The in-browser console
(`ENABLE_WEB_CONSOLE`) is optional and rides your `APP_PASSWORD` login when enabled.

Pin a release instead of `latest` for production:

```bash
TAG=v0.2.0 docker compose up -d
```

Log in to your subscription-plan CLIs once (auth persists on the `agent_home` volume):

```bash
docker compose exec -u sandbox -e HOME=/home/agent backend bash /app/scripts/auth.sh
# or a subset:  ... auth.sh claude grok
```

The dashboard is at `http://<host>:8080`.

Once signed in, you can also re-auth a plan from the frontend — **Settings → AI Plans**
surfaces each provider's health, headroom, and a re-auth action that runs the same login
flow in-browser. This requires the scoped in-UI console (`ENABLE_WEB_CONSOLE=true`); it is
off by default and never available in `managed` mode. The `docker compose exec` form above
always works regardless.

### Air-gapped / offline hosts

If the host can't pull from GHCR, transfer the images instead: `docker save` both
`ghcr.io/batonkeep/batonkeep-backend` and `-frontend` on a connected machine, copy the
archives over, and `docker load` them on the target. Then run with the same
`docker-compose.yml` + `.env` — with the images already present locally, `docker compose up -d`
(or `docker-compose up -d`) starts the stack without any pull.

## Putting it behind a public domain

The frontend is the only exposed port and the app is single-origin, so any TLS-terminating
proxy in front of that one port works — share/publish links pick up the public origin
automatically. Two common options:

### Cloudflare Tunnel (cloudflared) — no open inbound ports

Bind the web port to localhost so it is reachable only by the tunnel on the same host:

```bash
# .env
WEB_BIND=127.0.0.1
WEB_PORT=8080
```

Then point a tunnel at it (TLS is terminated at Cloudflare's edge):

```bash
cloudflared tunnel --url http://localhost:8080
# or, for a named tunnel, in config.yml:
#   ingress:
#     - hostname: batonkeep.example.com
#       service: http://localhost:8080
#     - service: http_status:404
```

WebSockets (live runs, build-session streaming) pass through cloudflared unchanged; nginx
forwards the `X-Forwarded-Proto` / `X-Forwarded-For` headers to the backend.

### Your own reverse proxy (Caddy / nginx / Traefik)

Terminate TLS at your proxy and forward everything to `http://<host>:8080`. Don't split
`/api` or `/ws` onto a different origin — the SPA, API, and WebSocket are intentionally one
origin. Leave `WEB_BIND=0.0.0.0` (or restrict to the proxy's network).

## Upgrades

Images carry the schema; the backend runs `alembic upgrade head` automatically at startup
(no manual migration step). To upgrade:

```bash
make pull && make up          # or: docker compose pull && docker compose up -d
```

Back up your volumes before a major upgrade — see **Data & backups** below.

## Data & backups

All of your state lives in two Docker named volumes. **If you lose them, it is gone — there
is no cloud copy.**

- **`appdata`** (`/data`) — the SQLite database (tasks, sessions, runs, encrypted credentials),
  your session workspaces, and task outputs. Losing this loses all your work.
- **`agent_home`** (`/home/agent`) — your plan-CLI OAuth logins. Losing this just means
  re-running the auth step for each provider — these are session tokens, not durable data.

### Recommended: the `batonkeep-backup` script

The backend ships a backup script that archives only your **durable, user-authored state** —
the database (including encrypted API keys), session workspaces, task outputs, and published
bundles. It **excludes regenerable bloat** (`node_modules`, Python virtualenvs, pip/npm caches,
build outputs — `node_modules` alone is typically the majority of workspace size) so the
archive stays small and portable. It does **not** prune anything else, so a restore never
forces you to reinstall a workspace's packages by hand.

Stream a backup straight to a file on your host (no copy step):

```bash
docker compose exec -T backend bash /app/scripts/batonkeep-backup.sh --stdout > batonkeep-backup.tar.gz
```

Preview exactly what would be included first with `--dry-run` (drop `-T` and `--stdout`):

```bash
docker compose exec backend bash /app/scripts/batonkeep-backup.sh --dry-run
```

To restore (onto a fresh install or new hardware) — **stop the stack first**, then pipe the
archive back in:

```bash
docker compose down                                    # stop the stack
docker compose up -d backend                           # bring just the backend up to restore into
cat batonkeep-backup.tar.gz | docker compose exec -T backend bash /app/scripts/batonkeep-restore.sh --stdin
docker compose up -d                                   # start everything
```

Two things to know about restore:

- **Provider logins are not in the backup.** After restoring, re-auth each plan-CLI provider:
  `docker compose exec -u sandbox -e HOME=/home/agent backend bash /app/scripts/auth.sh`.
- **Keep your `APP_SECRET`.** BYO API keys in the database are encrypted with `APP_SECRET` from
  `.env`. Restore onto an install whose `APP_SECRET` matches the backup-time value, or those
  keys can't be decrypted and must be re-entered in Settings. (Everything else restores cleanly
  regardless.)

This is also how you **move Batonkeep to new hardware**: back up on the old host, copy the one
`.tar.gz`, restore on the new one. (Continuous cross-machine *sync* is not part of self-hosting.)

### Alternative: raw volume snapshot

If you'd rather snapshot the volumes directly (e.g. as part of a host-level backup regime),
note this captures everything including the regenerable bloat the script skips:

```bash
docker run --rm -v batonkeep_appdata:/data -v "$PWD:/backup" alpine \
  tar czf /backup/appdata-$(date +%F).tar.gz -C /data .
docker run --rm -v batonkeep_agent_home:/home/agent -v "$PWD:/backup" alpine \
  tar czf /backup/agent_home-$(date +%F).tar.gz -C /home/agent .
```

(The `batonkeep_` prefix comes from `COMPOSE_PROJECT_NAME` in `.env`, which the install ships
set to `batonkeep` so these names are stable regardless of your install folder. If you changed
it, adjust the volume names to match — confirm with `docker volume ls`.) Restore by extracting
the archives back into fresh volumes before
`docker compose up -d`. **Note:** `docker compose down -v` deletes these volumes — never use
`-v` on a live install you care about.

## Optional: self-hosted open-weight inference

`docker compose --profile selfhost up -d` adds an Ollama container on the internal network.
Point a custom provider at `http://inference:11434/v1`.

Note: open-weight and other API-key providers connect through our own agent loop — a
**first-class agentic lane** (web search/fetch, filesystem read/search, Python code execution,
image generation, and image input on vision-capable models), now at **near-parity** with the
plan-CLI lane. Two edge differences remain: the plan-CLI lane draws on each vendor's full native
toolset (arbitrary external tools sit behind a trust boundary on the API lane), and failover
behaves a little differently per lane. Open-weight models are the right call for
sovereignty/offline work; capability between the lanes is close.
