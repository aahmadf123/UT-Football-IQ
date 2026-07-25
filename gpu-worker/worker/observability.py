"""Prometheus-style observability helpers for the Football-IQ GPU worker.

Provides:
  - Standard Prometheus counters, histograms, and gauges for job processing,
    R2 storage interactions, and heartbeat liveness.
  - A ``/metrics`` HTTP handler for the optional lightweight metrics server.

Metric cardinality is intentionally limited: labels use job_type and
pipeline_mode only — no unbounded IDs (clip_id, video_id, …) in any label.
"""

from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

SERVICE_NAME = "football-iq-gpu-worker"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

REGISTRY = CollectorRegistry()

# ── Job metrics ───────────────────────────────────────────────────────────────

JOBS_STARTED_TOTAL = Counter(
    "gpu_jobs_started_total",
    "Total jobs started by job_type and pipeline_mode.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOBS_SUCCEEDED_TOTAL = Counter(
    "gpu_jobs_succeeded_total",
    "Total jobs that completed successfully.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOBS_FAILED_TOTAL = Counter(
    "gpu_jobs_failed_total",
    "Total jobs that failed.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOBS_TIMED_OUT_TOTAL = Counter(
    "gpu_jobs_timed_out_total",
    "Total jobs that exceeded the timeout and were requeued.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOB_DURATION_SECONDS = Histogram(
    "gpu_job_duration_seconds",
    "Job processing latency in seconds.",
    ["job_type", "pipeline_mode", "service", "env"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 480, 600),
    registry=REGISTRY,
)

# ── Queue metrics ─────────────────────────────────────────────────────────────

QUEUE_POLL_TOTAL = Counter(
    "gpu_queue_poll_total",
    "Total queue poll attempts by outcome.",
    ["outcome", "service", "env"],
    registry=REGISTRY,
)

QUEUE_MESSAGES_RECEIVED = Counter(
    "gpu_queue_messages_received_total",
    "Total messages received from queue polls.",
    ["service", "env"],
    registry=REGISTRY,
)

# ── R2 / storage metrics ─────────────────────────────────────────────────────

R2_OPERATIONS_TOTAL = Counter(
    "gpu_r2_operations_total",
    "Total R2 storage operations by type and outcome.",
    ["operation", "outcome", "bucket", "service", "env"],
    registry=REGISTRY,
)

R2_OPERATION_DURATION_SECONDS = Histogram(
    "gpu_r2_operation_duration_seconds",
    "R2 operation latency in seconds.",
    ["operation", "bucket", "service", "env"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

# ── Heartbeat / liveness ─────────────────────────────────────────────────────

HEARTBEAT_TIMESTAMP = Gauge(
    "gpu_worker_heartbeat_timestamp",
    "Unix timestamp of the last heartbeat from the GPU worker.",
    ["service", "env"],
    registry=REGISTRY,
)

WORKER_UP = Gauge(
    "gpu_worker_up",
    "Set to 1 while the GPU worker main loop is running.",
    ["service", "env"],
    registry=REGISTRY,
)


# ── Convenience helpers ───────────────────────────────────────────────────────

def record_heartbeat() -> None:
    """Update the heartbeat gauge to the current time."""
    HEARTBEAT_TIMESTAMP.labels(service=SERVICE_NAME, env=ENVIRONMENT).set(time.time())


def set_worker_up(up: bool = True) -> None:
    WORKER_UP.labels(service=SERVICE_NAME, env=ENVIRONMENT).set(1 if up else 0)


def record_job_started(job_type: str, pipeline_mode: str) -> None:
    JOBS_STARTED_TOTAL.labels(
        job_type=job_type, pipeline_mode=pipeline_mode,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).inc()


def record_job_succeeded(job_type: str, pipeline_mode: str, duration_seconds: float) -> None:
    JOBS_SUCCEEDED_TOTAL.labels(
        job_type=job_type, pipeline_mode=pipeline_mode,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).inc()
    JOB_DURATION_SECONDS.labels(
        job_type=job_type, pipeline_mode=pipeline_mode,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).observe(duration_seconds)


def record_job_failed(job_type: str, pipeline_mode: str) -> None:
    JOBS_FAILED_TOTAL.labels(
        job_type=job_type, pipeline_mode=pipeline_mode,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).inc()


def record_job_timed_out(job_type: str, pipeline_mode: str) -> None:
    JOBS_TIMED_OUT_TOTAL.labels(
        job_type=job_type, pipeline_mode=pipeline_mode,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).inc()


def record_queue_poll(outcome: str, messages_received: int = 0) -> None:
    """Record a queue poll attempt.

    Args:
        outcome: ``success`` or ``error``.
        messages_received: Number of messages returned (only for success).
    """
    QUEUE_POLL_TOTAL.labels(outcome=outcome, service=SERVICE_NAME, env=ENVIRONMENT).inc()
    if messages_received > 0:
        QUEUE_MESSAGES_RECEIVED.labels(
            service=SERVICE_NAME, env=ENVIRONMENT,
        ).inc(messages_received)


def record_r2_operation(
    operation: str,
    bucket: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    R2_OPERATIONS_TOTAL.labels(
        operation=operation, outcome=outcome, bucket=bucket,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).inc()
    R2_OPERATION_DURATION_SECONDS.labels(
        operation=operation, bucket=bucket,
        service=SERVICE_NAME, env=ENVIRONMENT,
    ).observe(duration_seconds)


# ── Lightweight metrics HTTP server ───────────────────────────────────────────

class _MetricsHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that serves ``/metrics`` and ``/health``."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            body = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/health", "/live"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"football-iq-gpu-worker"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress default stderr logging


def start_metrics_server(port: int = 9090) -> threading.Thread:
    """Start a daemon thread serving Prometheus metrics on *port*.

    Returns the daemon thread.  The server runs until the process exits.
    """
    server = HTTPServer(("0.0.0.0", port), _MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="metrics-server")
    thread.start()
    return thread
