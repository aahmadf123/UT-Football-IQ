"""Tests for the DB-as-queue worker loop pieces in gpu-worker/__main__.py.

Covers claim/heartbeat wiring, the claimed-row → legacy job-dict mapping,
the pipeline job handler's terminal status updates, and the default-off CF
publish skip. All HTTP is mocked — no backend, queue, or GPU required.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)

# The worker entrypoint is literally named __main__.py, which collides with
# the running interpreter's __main__ — load it by path under a private name.
_spec = importlib.util.spec_from_file_location(
    "gpu_worker_main", os.path.join(_ROOT, "__main__.py")
)
assert _spec is not None and _spec.loader is not None
worker_main = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("gpu_worker_main", worker_main)
_spec.loader.exec_module(worker_main)


@pytest.fixture(autouse=True)
def _backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "BACKEND_API_URL", "http://backend.test")


def _response(status_code: int, payload: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.raise_for_status = MagicMock()
    return resp


# ── claim_db_job ──────────────────────────────────────────────────────────────


def test_claim_returns_none_on_204() -> None:
    client = MagicMock()
    client.post.return_value = _response(204)
    assert worker_main.claim_db_job(client) is None


def test_claim_returns_row_on_200() -> None:
    row = {"id": "j1", "job_type": "pipeline", "video_id": "v1", "priority": 0}
    client = MagicMock()
    client.post.return_value = _response(200, row)
    assert worker_main.claim_db_job(client) == row
    body = client.post.call_args.kwargs["json"]
    assert body["worker_id"] and body["lease_seconds"] > 0


def test_claim_invalidates_and_retries_on_401() -> None:
    client = MagicMock()
    client.post.side_effect = [_response(401), _response(204)]
    with patch("worker.auth.invalidate") as invalidate:
        assert worker_main.claim_db_job(client) is None
    invalidate.assert_called_once()
    assert client.post.call_count == 2


# ── _db_row_to_job ────────────────────────────────────────────────────────────


def test_row_mapping_prefers_input_artifacts_uri() -> None:
    job = worker_main._db_row_to_job(
        {
            "id": "j1",
            "job_type": "pipeline",
            "video_id": "v1",
            "priority": 10,
            "input_artifacts": {"input_uri": "local://raw-video/raw/a.mp4"},
        }
    )
    assert job["inputUri"] == "local://raw-video/raw/a.mp4"
    assert job["jobType"] == "pipeline"
    assert job["priority"] == 10


def test_row_mapping_accepts_camel_case_uri() -> None:
    job = worker_main._db_row_to_job(
        {
            "id": "j1",
            "job_type": "ingest",
            "video_id": "v1",
            "input_artifacts": {"inputUri": "r2://raw-video/raw/b.mp4"},
        }
    )
    assert job["inputUri"] == "r2://raw-video/raw/b.mp4"


def test_row_mapping_falls_back_to_video_storage_uri() -> None:
    with patch.object(
        worker_main, "_fetch_video", return_value={"storage_uri": "local://raw-video/raw/c.mp4"}
    ) as fetch:
        job = worker_main._db_row_to_job(
            {"id": "j1", "job_type": "pipeline", "video_id": "v9", "input_artifacts": None}
        )
    fetch.assert_called_once_with("v9")
    assert job["inputUri"] == "local://raw-video/raw/c.mp4"


# ── process_pipeline_job ──────────────────────────────────────────────────────


def _pipeline_job() -> dict[str, Any]:
    return {
        "jobId": "job-1",
        "jobType": "pipeline",
        "videoId": "vid-1",
        "clipId": "",
        "inputUri": "file:///tmp/x.mp4",
        "priority": 0,
        "inputArtifacts": {},
    }


def test_pipeline_job_success_patches_summary() -> None:
    summary = {"clip_count": 2, "video_id": "vid-1"}
    with (
        patch("pipeline.orchestrator.run_pipeline", return_value=summary) as run,
        patch.object(worker_main, "_update_job_status") as update,
        patch.object(worker_main, "heartbeat_db_job"),
    ):
        worker_main.process_pipeline_job(_pipeline_job())
    run.assert_called_once()
    update.assert_called_once_with("job-1", "succeeded", output_artifacts=summary)


def test_pipeline_job_failure_patches_failed() -> None:
    with (
        patch("pipeline.orchestrator.run_pipeline", side_effect=RuntimeError("boom")),
        patch.object(worker_main, "_update_job_status") as update,
        patch.object(worker_main, "heartbeat_db_job"),
    ):
        worker_main.process_pipeline_job(_pipeline_job())
    update.assert_called_once_with("job-1", "failed", error_message="boom")


def test_pipeline_job_without_uri_fails_fast() -> None:
    job = _pipeline_job() | {"inputUri": ""}
    with patch.object(worker_main, "_update_job_status") as update:
        worker_main.process_pipeline_job(job)
    update.assert_called_once_with("job-1", "failed", error_message="no input_uri resolvable")


def test_process_job_routes_pipeline_type() -> None:
    with patch.object(worker_main, "process_pipeline_job") as handler:
        worker_main.process_job(_pipeline_job())
    handler.assert_called_once()


# ── CF publish default-off ────────────────────────────────────────────────────


def test_cf_push_skipped_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CF_QUEUES_ENABLED", raising=False)
    from queue import same_session_queue

    with patch("httpx.Client") as client_cls:
        msg_id = same_session_queue.push_nightly_job({"jobId": "j1"})
    assert msg_id == "db-only"
    client_cls.assert_not_called()
