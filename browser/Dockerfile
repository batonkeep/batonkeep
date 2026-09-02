# Batonkeep browser sidecar ([[D-0073]] D1a) — the optional headless-browser image.
#
# Separate from the backend on purpose. Shipping Chromium in the backend image cost
# +2.1 GB (3.41 -> 5.49 GB) for a capability that defaults to `off`; here, only an
# operator who runs `--profile browser` pays for it.
#
# The upstream Playwright image is the right base: it carries the exact browser build
# the pinned playwright version expects plus the system libraries it needs, which is the
# part that fails at *runtime* with an opaque loader error when assembled by hand.
FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

ARG BATONKEEP_VERSION=dev
ENV BATONKEEP_VERSION=${BATONKEEP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Pinned, and deliberately tiny: this service has one endpoint. Every dependency here is
# reachable by whatever the browser renders, so the list stays as short as it can be.
RUN pip install --no-cache-dir \
      "fastapi==0.140.0" \
      "uvicorn==0.51.0" \
      "pydantic==2.13.4"

COPY server.py /app/server.py

# Run as the image's unprivileged user. Chromium's own sandbox needs to work, and root
# would silently push people toward `--no-sandbox`.
USER pwuser

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3000"]
