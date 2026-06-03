.PHONY: up down logs build auth auth-shell seed shell fmt selfhost sync dev-backend dev-frontend test push build-base prod-up prod-down prod-logs backup restore

# ── Core ──────────────────────────────────────────────────────────────────────

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

# ── Developer shortcuts ───────────────────────────────────────────────────────

## Guided plan-CLI login walkthrough inside the backend container. Logins persist
## on the agent_home volume; auth-once, run forever. Falls back to manual-install
## hints for any CLI not present in the image.
##   make auth               # all providers (claude, grok, agy, codex)
##   make auth p=claude      # only one
##   make auth p="grok agy"  # a subset
##   make auth p=claude:work # an extra instance (declared in PROVIDER_INSTANCES_CONFIG)
auth:
	docker compose exec backend bash /app/scripts/auth.sh $(p)

## Drop into a raw shell in the backend container (manual CLI install/login).
auth-shell:
	docker compose exec backend bash

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

# ── Registry / Deployment ────────────────────────────────────────────────────

## Build the heavy base image (OS + Node + CLI agents + frozen Python venv).
## Required before the first `make push`.
build-base:
	docker compose --profile build-base build backend-base

## Push backend-base and frontend to the Cloudflare-proxied registry.
## Requires: REGISTRY (e.g. registry.example.com/batonkeep) and optionally TAG.
##   make push REGISTRY=registry.example.com/batonkeep TAG=v1.2.0
push:
	./scripts/push_to_registry.sh $(REGISTRY) $(TAG)

## Start the production stack (images from registry, backend built on-host).
## Requires: REGISTRY_PREFIX and TAG env vars.
prod-up:
	docker compose -f docker-compose.prod.yml build backend
	docker compose -f docker-compose.prod.yml up -d

prod-down:
	docker compose -f docker-compose.prod.yml down

prod-logs:
	docker compose -f docker-compose.prod.yml logs -f

# ── Volume backup / restore ───────────────────────────────────────────────────

## Backup appdata and agent_home volumes to ./backups/ (stops containers).
## Pass NO_STOP=1 for a live backup: make backup NO_STOP=1
backup:
	$(if $(NO_STOP),./scripts/backup.sh --no-stop,./scripts/backup.sh)

## Restore volumes from a backup archive. Requires FILE=<path>.
## Add YES=1 to skip confirmation: make restore FILE=./backups/... YES=1
restore:
	$(if $(YES),./scripts/restore.sh --yes $(FILE),./scripts/restore.sh $(FILE))
