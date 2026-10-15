# syntax=docker/dockerfile:1
# Build from the repository root: docker build -f backend/Dockerfile .
# The API and CPU pipeline share the existing Cloudflare Container, with
# isolated Python environments and processes. The API never imports models.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    python -m venv /opt/api && \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi && \
    /opt/api/bin/pip install -r /tmp/backend-requirements.txt

COPY gpu-worker/requirements.txt gpu-worker/requirements-cpu.txt /tmp/worker/
RUN --mount=type=secret,id=proxy_ca \
    python -m venv /opt/worker && \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi && \
    /opt/worker/bin/pip install -r /tmp/worker/requirements-cpu.txt

# Download the existing baseline into the image, never into Git.
# The installer verifies the exact published asset SHA256 before accepting it.
COPY gpu-worker/scripts/install_pretrained.py /tmp/install_pretrained.py
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi && \
    python /tmp/install_pretrained.py --output /opt/models/yolov8n.pt

FROM deps AS app
WORKDIR /app
COPY backend/ /app/
COPY gpu-worker/ /worker/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser && \
    mkdir -p /app/data/storage /app/data/checkpoints /home/appuser/.config/ultralytics && \
    chmod -R a+rX /app /worker && \
    chown -R appuser:appgroup /app/data /worker /home/appuser
USER appuser

ENV PIPELINE_DEVICE=cpu \
    PIPELINE_CPU_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    MODEL_DETECT_PATH=/opt/models/yolov8n.pt \
    PIPELINE_CHECKPOINT_ROOT=/app/data/checkpoints \
    PIPELINE_STORAGE_TRANSPORT=api \
    YOLO_CONFIG_DIR=/home/appuser/.config/ultralytics

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# The supervisor keeps exactly one uvicorn process (SSE subscriber ownership),
# starts a separate inference process, and lets it pause jobs before API exit.
CMD ["python", "/app/scripts/hosted_runtime.py"]
