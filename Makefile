.PHONY: up down logs build pull auth auth-shell seed shell fmt selfhost sync dev-backend dev-frontend test backup restore

# ── Core ──────────────────────────────────────────────────────────────────────

## Start the stack. From a repo clone, docker-compose.override.yml builds the
## images locally; with only docker-compose.yml present it pulls from GHCR.
## Dashboard at http://localhost:${WEB_PORT:-8080}.
up:
	docker compose up -d

## Pull the latest released images from GHCR (production upgrade path; pair with
## the alembic migration that runs automatically at backend startup).
##   make pull TAG=v0.1.0 && make up
pull:
	docker compose pull

down:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

# ── Developer shortcuts ───────────────────────────────────────────────────────

# Auth/login must run as the SAME identity the agent CLIs run as at runtime —
# the low-privilege `sandbox` user with HOME=/home/agent. A plain
# `docker compose exec` runs as root with HOME=/home/batond, so logins would write
# root-owned token files (or to the wrong HOME entirely), leaving the sandbox agent
# unable to read its own auth. Pin both here so credentials land where the agent
# reads them and stay sandbox-owned.
EXEC_AS_AGENT = docker compose exec -u sandbox -e HOME=/home/agent backend

## Guided plan-CLI login walkthrough inside the backend container. Logins persist
## on the agent_home volume; auth-once, run forever. Falls back to manual-install
## hints for any CLI not present in the image.
##   make auth               # all providers (claude, grok, agy, codex)
##   make auth p=claude      # only one
##   make auth p="grok agy"  # a subset
##   make auth p=claude:work # an extra instance (declared in PROVIDER_INSTANCES_CONFIG)
auth:
	$(EXEC_AS_AGENT) bash /app/scripts/auth.sh $(p)

## Drop into a raw shell AS THE SANDBOX AGENT (manual CLI login / inspect auth).
## For root tasks like installing a CLI globally, use `make shell` instead.
auth-shell:
	$(EXEC_AS_AGENT) bash

## Re-insert seed tasks (safe to run multiple times — inserts only if table empty).
seed:
	docker compose exec backend python -m app.seed

shell:
	docker compose exec backend bash

fmt:
	docker compose exec backend uvx ruff format app/

fmt-local:
	cd backend && uvx ruff format app/

lint-local:
	cd backend && uvx ruff check app/

# ── Self-hosted inference (optional GPU) ──────────────────────────────────────

selfhost:
	docker compose --profile selfhost up -d

# ── Local dev (no Docker) ─────────────────────────────────────────────────────

## Install dev deps locally with uv
sync:
	cd backend && uv sync

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend:
	cd frontend && npm run dev

# ── Testing ───────────────────────────────────────────────────────────────────

test:
	cd backend && uv run pytest tests/ -v

# ── Release / Deployment ─────────────────────────────────────────────────────
# Images are built and pushed to GHCR by CI on a version tag — see
# .github/workflows/release.yml and docs/self-hosting.md. There is no manual
# push step and no on-host base build; production runs the published images
# (docker-compose.yml) and upgrades with `make pull && make up`.

# ── Volume backup / restore ───────────────────────────────────────────────────

## Backup appdata and agent_home volumes to ./backups/ (stops containers).
## Pass NO_STOP=1 for a live backup: make backup NO_STOP=1
backup:
	$(if $(NO_STOP),./scripts/backup.sh --no-stop,./scripts/backup.sh)

## Restore volumes from a backup archive. Requires FILE=<path>.
## Add YES=1 to skip confirmation: make restore FILE=./backups/... YES=1
restore:
	$(if $(YES),./scripts/restore.sh --yes $(FILE),./scripts/restore.sh $(FILE))
