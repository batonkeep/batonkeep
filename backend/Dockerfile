# ── Backend image (single multi-stage build) ─────────────────────────────────
# Built and pushed complete by CI to GHCR (amd64 + arm64). GHCR has no anonymous
# pull-rate limits and no per-image bandwidth penalty, so — unlike the former
# Cloudflare-proxied registry — the full image (venv + Node + CLI agents) ships
# in one artifact. There is no separate on-host runtime layer and no base image
# to pre-push; stage `pyenv` below replaces the old Dockerfile.base.
#
# Build-time stage split (not a registry artifact):
#   stage `pyenv` : python:slim + OS deps + setuid helper + frozen venv
#   final stage   : the above + Node 20 + claude / grok / agy / codex + app code

# ── Stage 1: python env + privilege model + frozen venv ───────────────────────
FROM python:3.12-slim AS pyenv

# ── uv ────────────────────────────────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_SYSTEM_PYTHON=1 \
    UV_NO_CACHE=1

# ── Privilege separation: batond (control plane) vs sandbox (agent CLIs) ────────
# P-0022 / D-0020. The backend runs as `batond`; agent CLIs are launched as the
# low-privilege `sandbox` user via the setuid helper below, so kernel DAC fences
# the agent off from /app and control-plane /data (it cannot read what it does
# not own). `sandbox` is created FIRST so it inherits uid 1000 — the uid the
# former single `agent` user held — keeping the existing agent_home OAuth volume
# and per-instance CLI auth dirs valid without a chown. Its home stays
# /home/agent (the agent_home mount path) for the same reason.
# A shared `agents` group lets batond (git init/commit/restore) and sandbox
# (file edits) co-write session/work trees; setgid dirs + umask 002 preserve it.
RUN groupadd agents \
 && useradd -u 1000 -m -d /home/agent -s /bin/bash -G agents sandbox \
 && useradd -m -d /home/batond -s /bin/bash -G agents batond
# NOTE: deliberately do NOT bake `ENV HOME=...`. A container-wide HOME leaks into
# every `docker exec -u sandbox` and into agent subprocesses, overriding sandbox's
# real home (/home/agent) with batond's — so plan-CLI auth landed in batond's
# ephemeral home and vanished on rebuild. With no ENV HOME, Docker resolves HOME
# from /etc/passwd per user (sandbox→/home/agent, batond→/home/batond). The backend
# process is given HOME=/home/batond explicitly by entrypoint.sh.

# ── setuid privilege-drop helper (the single privileged surface) ───────────────
# Built here; installed 4750 root:batond so ONLY the backend (batond group) may
# exec it. Drops to `sandbox` and execs the agent CLI; also reaps web-TTY trees.
COPY sandbox-spawn.c /tmp/sandbox-spawn.c
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
 && gcc -O2 -Wall -Wextra -o /usr/local/bin/sandbox-spawn /tmp/sandbox-spawn.c \
 && chown root:batond /usr/local/bin/sandbox-spawn \
 && chmod 4750 /usr/local/bin/sandbox-spawn \
 && rm -f /tmp/sandbox-spawn.c \
 && apt-get purge -y gcc libc6-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python venv (frozen from lockfile) ─────────────────────────────────────────
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

# ── Data directories + ownership ───────────────────────────────────────────────
# Control plane (/app, /data root, the DB) is batond-owned and unreadable to
# sandbox. /work (task workspaces) and /data/sessions (build sessions) are
# agents-group, setgid, group-writable — the shared lane both users co-write.
RUN mkdir -p /data /data/sessions /work \
 && chown -R batond:batond /app /data \
 && chown batond:agents /data/sessions /work \
 && chmod 2770 /data/sessions /work

# ── Final stage: Node 20 + CLI agents + application code ───────────────────────
FROM pyenv

# ── Node 20 + CLI agents ───────────────────────────────────────────────────────
# Each RUN is isolated so a best-effort failure (subscription-gated betas, etc.)
# doesn't abort the build. Manual `make auth` activates subscriptions.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git gnupg gosu \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# claude / grok — npm global
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || true
RUN npm install -g @xai-official/grok 2>/dev/null || true
# wrangler — Cloudflare Pages deploy connector (backend-side publish target).
# Invoked non-interactively with CLOUDFLARE_API_TOKEN/ACCOUNT_ID; never in a sandbox.
RUN npm install -g wrangler 2>/dev/null || true
# agy + codex — native binaries; override HOME so they land under /usr/local
# (never shadowed by the agent_home volume), then symlink into /usr/local/bin.
RUN (export HOME=/usr/local; curl -fsSL https://antigravity.google/cli/install.sh | bash) 2>/dev/null \
    && ln -sf /usr/local/.local/bin/agy /usr/local/bin/agy \
    || true
RUN (export HOME=/usr/local; curl -fsSL https://chatgpt.com/codex/install.sh | sh) 2>/dev/null \
    && ln -sf /usr/local/.local/bin/codex /usr/local/bin/codex \
    || true

# ── Application code ───────────────────────────────────────────────────────────
# Owned by batond (control plane), unreadable to the sandbox user — P-0022/D-0020.
COPY --chown=batond:batond app/ app/
COPY --chown=batond:batond scripts/ scripts/
# Alembic migration scripts + config — init_db() runs `upgrade head` at startup (D-0021).
COPY --chown=batond:batond alembic.ini ./
COPY --chown=batond:batond alembic/ alembic/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x scripts/*.sh /usr/local/bin/entrypoint.sh

# Start as root so the entrypoint can run the one-shot volume-ownership migration
# (P-0022/D-0020), then it execs the backend as the non-root `batond` via gosu.
# Only this brief init is privileged; the network-facing process never is.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
