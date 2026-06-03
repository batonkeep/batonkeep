# ── Runtime layer ─────────────────────────────────────────────────────────────
# Built on the target host from the registry base; NEVER pushed to the registry.
#
# Layer split rationale:
#   base (pushed, ~80 MB compressed): python:slim + system deps + frozen venv
#   this file (on-host only, ~450 MB delta): Node 20 + claude-code + grok + agy + codex
#
# The CLI agents are what balloon the image. Keeping them here means they never
# transit the Cloudflare-proxied registry.

ARG REGISTRY_PREFIX=""
ARG TAG=latest
FROM ${REGISTRY_PREFIX}batonkeep-backend-base:${TAG}

# ── Node 20 + CLI agents ───────────────────────────────────────────────────────
# Each RUN is isolated so a best-effort failure (subscription-gated betas, etc.)
# doesn't abort the build. Manual `make auth` activates subscriptions.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# claude / grok — npm global
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || true
RUN npm install -g @xai-official/grok 2>/dev/null || true
# agy + codex — native binaries; override HOME so they land under /usr/local
# (never shadowed by the agent_home volume), then symlink into /usr/local/bin.
RUN (export HOME=/usr/local; curl -fsSL https://antigravity.google/cli/install.sh | bash) 2>/dev/null \
    && ln -sf /usr/local/.local/bin/agy /usr/local/bin/agy \
    || true
RUN (export HOME=/usr/local; curl -fsSL https://chatgpt.com/codex/install.sh | sh) 2>/dev/null \
    && ln -sf /usr/local/.local/bin/codex /usr/local/bin/codex \
    || true

# ── Application code ───────────────────────────────────────────────────────────
COPY --chown=agent:agent app/ app/
COPY --chown=agent:agent scripts/ scripts/
RUN chmod +x scripts/*.sh

USER agent

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
