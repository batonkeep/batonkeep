# Self-hosting Batonkeep

Batonkeep runs as two containers from prebuilt images on [GHCR](https://github.com/orgs/batonkeep/packages):
a backend (control plane + agent CLIs) and a frontend (nginx serving the SPA and
reverse-proxying the API/WebSocket). **Only the frontend is exposed** — the backend is
reachable only on the internal compose network. That single web port is the one surface you
put a domain and TLS in front of.

## Host requirements

- **Docker Engine** 24+ with the Compose v2 plugin (`docker compose`, not `docker-compose`).
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
# edit .env — at minimum set APP_SECRET (random 64-hex); pick DEFAULT_CANDIDATES after auth
docker compose up -d
```

Pin a release instead of `latest` for production:

```bash
TAG=v0.1.0 docker compose up -d
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

Back up the `appdata` volume (it holds the SQLite DB and outputs) before a major upgrade.

## Optional: self-hosted open-weight inference

`docker compose --profile selfhost up -d` adds an Ollama container on the internal network.
Point a custom provider at `http://inference:11434/v1`.
