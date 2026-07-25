"""Tests for GPU worker observability helpers."""

from prometheus_client import generate_latest

from worker.observability import (
    REGISTRY,
    record_heartbeat,
    record_job_failed,
    record_job_started,
    record_job_succeeded,
    record_job_timed_out,
    record_queue_poll,
    record_s3_operation,
    set_worker_up,
)


def test_job_lifecycle_metrics() -> None:
    record_job_started("detect", "same_session")
    record_job_succeeded("detect", "same_session", 5.0)
    record_job_failed("track", "nightly")
    record_job_timed_out("render", "same_session")

    output = generate_latest(REGISTRY).decode()
    assert "gpu_jobs_started_total" in output
    assert "gpu_jobs_succeeded_total" in output
    assert "gpu_jobs_failed_total" in output
    assert "gpu_jobs_timed_out_total" in output
    assert "gpu_job_duration_seconds" in output


def test_queue_poll_metrics() -> None:
    record_queue_poll("success", 3)
    record_queue_poll("error")

    output = generate_latest(REGISTRY).decode()
    assert "gpu_queue_poll_total" in output
    assert "gpu_queue_messages_received_total" in output


def test_s3_operation_metrics() -> None:
    record_s3_operation("download", "football-iq", "success", 1.5)
    record_s3_operation("upload", "football-iq", "error", 0.3)

    output = generate_latest(REGISTRY).decode()
    assert "gpu_s3_operations_total" in output
    assert "gpu_s3_operation_duration_seconds" in output


def test_heartbeat_and_worker_up() -> None:
    record_heartbeat()
    set_worker_up(True)

    output = generate_latest(REGISTRY).decode()
    assert "gpu_worker_heartbeat_timestamp" in output
    assert "gpu_worker_up" in output

    set_worker_up(False)
