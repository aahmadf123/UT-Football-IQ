"""Prometheus-style observability helpers for the Football-IQ backend.

Provides:
  - Standard Prometheus counters, histograms, and gauges for API requests,
    job execution, and object-store interactions.
  - A ``/metrics`` endpoint compatible with any Prometheus scraper.
  - A ``PrometheusMiddleware`` that auto-instruments every HTTP request.

Metric cardinality is intentionally limited: labels use route *templates*
(e.g. ``/api/v1/clips/{clip_id}``) rather than concrete path values, and
no unbounded IDs (clip_id, user_id, …) appear in any label.
"""

from __future__ import annotations

import os
import time

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

SERVICE_NAME = "football-iq-backend"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

# Use the default registry so all metrics are globally discoverable.
REGISTRY = CollectorRegistry()

# ── API request metrics ───────────────────────────────────────────────────────

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests by method, route template, and status code.",
    ["method", "route", "status_code", "service", "env"],
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds by method and route template.",
    ["method", "route", "service", "env"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed.",
    ["method", "route", "service", "env"],
    registry=REGISTRY,
)

# ── Job metrics ───────────────────────────────────────────────────────────────

JOBS_STARTED_TOTAL = Counter(
    "jobs_started_total",
    "Total jobs started by job_type and pipeline_mode.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOBS_SUCCEEDED_TOTAL = Counter(
    "jobs_succeeded_total",
    "Total jobs that completed successfully.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOBS_FAILED_TOTAL = Counter(
    "jobs_failed_total",
    "Total jobs that failed.",
    ["job_type", "pipeline_mode", "service", "env"],
    registry=REGISTRY,
)

JOB_DURATION_SECONDS = Histogram(
    "job_duration_seconds",
    "Job processing latency in seconds.",
    ["job_type", "pipeline_mode", "service", "env"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 480, 600),
    registry=REGISTRY,
)

# ── Object storage metrics ─────────────────────────────────────────────────────

S3_OPERATIONS_TOTAL = Counter(
    "s3_operations_total",
    "Total object-store operations by operation type and outcome.",
    ["operation", "outcome", "bucket", "service", "env"],
    registry=REGISTRY,
)

S3_OPERATION_DURATION_SECONDS = Histogram(
    "s3_operation_duration_seconds",
    "Object-store operation latency in seconds.",
    ["operation", "bucket", "service", "env"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _route_template(request: Request) -> str:
    """Return the matched route template or fall back to the raw path.

    FastAPI populates ``request.scope["route"]`` for matched routes; for
    unmatched paths (404s, static files, etc.) we bucket everything under
    ``/unmatched`` to avoid cardinality explosion.
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    return "/unmatched"


def metrics_response() -> Response:
    """Render all registered metrics in Prometheus text exposition format."""
    body = generate_latest(REGISTRY)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# ── Starlette middleware ──────────────────────────────────────────────────────


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Auto-instruments every HTTP request with Prometheus counters and histograms."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Skip instrumentation for the metrics endpoint itself.
        if request.url.path == "/metrics":
            return await call_next(request)

        method = request.method
        start = time.perf_counter()

        # Use a placeholder for in-progress tracking since route template
        # is not resolved until after call_next.
        in_progress = HTTP_REQUESTS_IN_PROGRESS.labels(
            method=method,
            route="pending",
            service=SERVICE_NAME,
            env=ENVIRONMENT,
        )
        in_progress.inc()
        try:
            response = await call_next(request)
        finally:
            in_progress.dec()

        duration = time.perf_counter() - start
        route = _route_template(request)
        status = str(response.status_code)

        HTTP_REQUESTS_TOTAL.labels(
            method=method,
            route=route,
            status_code=status,
            service=SERVICE_NAME,
            env=ENVIRONMENT,
        ).inc()

        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=method,
            route=route,
            service=SERVICE_NAME,
            env=ENVIRONMENT,
        ).observe(duration)

        return response


# ── Convenience: record object-store operations ─────────────────────────────────────────


def record_s3_operation(
    operation: str,
    bucket: str,
    outcome: str,
    duration_seconds: float,
    service: str = SERVICE_NAME,
) -> None:
    """Record a single object-store operations for metrics.

    Args:
        operation: One of ``upload``, ``download``, ``upload_bytes``, ``list``, ``delete``.
        bucket: The bucket name.
        outcome: ``success`` or ``error``.
        duration_seconds: Wall-clock time of the operation.
        service: Originating service label (defaults to backend).
    """
    S3_OPERATIONS_TOTAL.labels(
        operation=operation,
        outcome=outcome,
        bucket=bucket,
        service=service,
        env=ENVIRONMENT,
    ).inc()

    S3_OPERATION_DURATION_SECONDS.labels(
        operation=operation,
        bucket=bucket,
        service=service,
        env=ENVIRONMENT,
    ).observe(duration_seconds)


# ── Convenience: record job lifecycle events ──────────────────────────────────


def record_job_started(job_type: str, pipeline_mode: str, service: str = SERVICE_NAME) -> None:
    JOBS_STARTED_TOTAL.labels(
        job_type=job_type,
        pipeline_mode=pipeline_mode,
        service=service,
        env=ENVIRONMENT,
    ).inc()


def record_job_succeeded(
    job_type: str,
    pipeline_mode: str,
    duration_seconds: float,
    service: str = SERVICE_NAME,
) -> None:
    JOBS_SUCCEEDED_TOTAL.labels(
        job_type=job_type,
        pipeline_mode=pipeline_mode,
        service=service,
        env=ENVIRONMENT,
    ).inc()
    JOB_DURATION_SECONDS.labels(
        job_type=job_type,
        pipeline_mode=pipeline_mode,
        service=service,
        env=ENVIRONMENT,
    ).observe(duration_seconds)


def record_job_failed(job_type: str, pipeline_mode: str, service: str = SERVICE_NAME) -> None:
    JOBS_FAILED_TOTAL.labels(
        job_type=job_type,
        pipeline_mode=pipeline_mode,
        service=service,
        env=ENVIRONMENT,
    ).inc()
