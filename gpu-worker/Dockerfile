# syntax=docker/dockerfile:1
# CPU-first worker. Optional CUDA image:
# --build-arg PYTORCH_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
# --build-arg WORKER_REQUIREMENTS=requirements.txt

ARG PYTORCH_IMAGE=python:3.11-slim
FROM ${PYTORCH_IMAGE} AS base
ARG WORKER_REQUIREMENTS=requirements-cpu.txt

LABEL maintainer="Toledo Football Analytics"
LABEL org.opencontainers.image.source="https://github.com/aahmadf123/Football-IQ"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive
ENV PIPELINE_DEVICE=cpu

# ── System dependencies (FFmpeg with NVDEC/NVENC) ─────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt requirements-cpu.txt ./
RUN pip install --no-cache-dir -r ${WORKER_REQUIREMENTS}

# ── Worker code ───────────────────────────────────────────────────────────────
COPY . .

# Run as non-root, but ensure the working directory is writable so runtime
# model downloads (e.g. Ultralytics .pt weights) can succeed.
RUN addgroup --system gpugroup && adduser --system --ingroup gpugroup gpuuser \
    && mkdir -p /worker /home/gpuuser && chown -R gpuuser:gpugroup /worker /home/gpuuser
USER gpuuser

CMD ["python", "__main__.py"]
