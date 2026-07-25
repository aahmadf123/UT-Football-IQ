# syntax=docker/dockerfile:1
# GPU worker image for Football-IQ video processing pipeline.
# Default: PyTorch 2.5.1 + CUDA 12.4. Override with --build-arg for newer GPUs
# (e.g. RTX 50-series needs a CUDA 12.8+ base image such as pytorch/pytorch:2.7.0-cuda12.8-cudnn91-runtime).

ARG PYTORCH_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
FROM ${PYTORCH_IMAGE} AS base

LABEL maintainer="Toledo Football Analytics"
LABEL org.opencontainers.image.source="https://github.com/aahmadf123/Football-IQ"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# ── System dependencies (FFmpeg with NVDEC/NVENC) ─────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Worker code ───────────────────────────────────────────────────────────────
COPY . .

# Run as non-root, but ensure the working directory is writable so runtime
# model downloads (e.g. Ultralytics .pt weights) can succeed.
RUN addgroup --system gpugroup && adduser --system --ingroup gpugroup gpuuser \
    && mkdir -p /worker /home/gpuuser && chown -R gpuuser:gpugroup /worker /home/gpuuser
USER gpuuser

CMD ["python", "__main__.py"]
