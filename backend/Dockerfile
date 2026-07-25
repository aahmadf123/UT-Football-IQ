# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

LABEL maintainer="Toledo Football Analytics"
LABEL org.opencontainers.image.source="https://github.com/aahmadf123/Football-IQ"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── Dependencies ─────────────────────────────────────────────────────────────
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application ───────────────────────────────────────────────────────────────
FROM deps AS app
COPY . .

# Run as non-root
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser && \
    mkdir -p /app/data/storage && chown -R appuser:appgroup /app/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Deliberately ONE worker.
#
# `app/routers/alerts_sse.py` keeps its subscriber map in process memory. With
# --workers 4, uvicorn forks four processes with four independent maps, so an
# alert published by the process handling the POST is never delivered to a coach
# whose GET /alerts/stream landed on one of the other three. The bug is silent:
# the stream stays open and simply never emits.
#
# Running one container instance is therefore necessary but not sufficient --
# the fork has to go too. Raising this is safe only once that map moves to a
# Durable Object or the frontend falls back to polling.
#
# Capacity is not the constraint it looks like: the app is async and I/O-bound,
# so a single worker serves a coaching staff comfortably. The one CPU-bound path
# is PDF report rendering, which runs in a background task and briefly occupies
# the event loop.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
