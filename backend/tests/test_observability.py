"""Tests for the observability module and /metrics endpoint."""

from app.observability import (
    REGISTRY,
    record_job_failed,
    record_job_started,
    record_job_succeeded,
    record_s3_operation,
)
from fastapi.testclient import TestClient
from prometheus_client import generate_latest


def test_metrics_endpoint_returns_prometheus_format(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    # Verify some expected metric families exist
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_record_job_lifecycle() -> None:
    record_job_started("detect", "nightly")
    record_job_succeeded("detect", "nightly", 12.5)
    record_job_failed("detect", "nightly")

    output = generate_latest(REGISTRY).decode()
    assert "jobs_started_total" in output
    assert "jobs_succeeded_total" in output
    assert "jobs_failed_total" in output


def test_record_s3_operation() -> None:
    record_s3_operation("upload", "test-bucket", "success", 0.5)
    record_s3_operation("download", "test-bucket", "error", 1.2)

    output = generate_latest(REGISTRY).decode()
    assert "s3_operations_total" in output
    assert "s3_operation_duration_seconds" in output
