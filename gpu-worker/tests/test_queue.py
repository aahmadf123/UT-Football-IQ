"""Unit tests for gpu-worker/queue/same_session_queue.py and job_dispatch.py.

Covers:
  - Priority constants are the values written to processing_jobs.priority
  - dispatch_on_upload uses same-session priority when is_same_session=True
  - dispatch_on_upload uses nightly priority when is_same_session=False
  - dispatch_on_upload POSTs a claimable job row to the backend
  - dispatch_on_upload forwards clip_id and input_artifacts
  - dispatch_on_upload raises when no backend is configured
  - dispatch_on_upload reports created=False when the insert fails
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

# Ensure gpu-worker root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


BACKEND_URL = "http://backend.test"


@pytest.fixture(autouse=True)
def _backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_API_URL", BACKEND_URL)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_client(status_code: int = 201) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.post.return_value = resp
    return client


def _posted_body(client: MagicMock) -> dict:
    _args, kwargs = client.post.call_args
    return dict(kwargs["json"])


# ── Priority constants ────────────────────────────────────────────────────────


class TestPriorities:
    def test_same_session_outranks_nightly(self) -> None:
        """The claim query orders by priority DESC, so same-session must be higher."""
        from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY

        assert SAME_SESSION_PRIORITY == 10
        assert NIGHTLY_PRIORITY == 0
        assert SAME_SESSION_PRIORITY > NIGHTLY_PRIORITY


# ── job_dispatch tests ────────────────────────────────────────────────────────


class TestDispatchOnUpload:
    def test_same_session_uses_same_session_priority(self) -> None:
        from queue.job_dispatch import dispatch_on_upload
        from queue.same_session_queue import SAME_SESSION_PRIORITY

        client = _mock_client()
        result = dispatch_on_upload(
            video_id="vid-1",
            storage_uri="local://raw-video/vid-1.mp4",
            is_same_session=True,
            client=client,
        )

        assert result["priority"] == SAME_SESSION_PRIORITY
        assert result["created"] is True
        body = _posted_body(client)
        assert body["priority"] == SAME_SESSION_PRIORITY
        assert body["pipeline_mode"] == "same_session"

    def test_nightly_uses_nightly_priority(self) -> None:
        from queue.job_dispatch import dispatch_on_upload
        from queue.same_session_queue import NIGHTLY_PRIORITY

        client = _mock_client()
        result = dispatch_on_upload(
            video_id="vid-2",
            storage_uri="local://raw-video/vid-2.mp4",
            is_same_session=False,
            client=client,
        )

        assert result["priority"] == NIGHTLY_PRIORITY
        body = _posted_body(client)
        assert body["priority"] == NIGHTLY_PRIORITY
        assert body["pipeline_mode"] == "nightly"

    def test_posts_claimable_job_row(self) -> None:
        """The processing_jobs row IS the queue entry — it must reach the API."""
        from queue.job_dispatch import dispatch_on_upload

        client = _mock_client()
        result = dispatch_on_upload(
            video_id="vid-3",
            storage_uri="local://raw-video/vid-3.mp4",
            job_type="ingest",
            client=client,
        )

        url = client.post.call_args[0][0]
        assert url == f"{BACKEND_URL}/api/v1/jobs"
        body = _posted_body(client)
        assert body["id"] == result["job_id"]
        assert body["video_id"] == "vid-3"
        assert body["job_type"] == "ingest"
        assert body["input_artifacts"]["inputUri"] == "local://raw-video/vid-3.mp4"

    def test_forwards_clip_id_and_artifacts(self) -> None:
        from queue.job_dispatch import dispatch_on_upload

        client = _mock_client()
        dispatch_on_upload(
            video_id="vid-4",
            storage_uri="local://raw-video/vid-4.mp4",
            clip_id="clip-9",
            input_artifacts={"fps": 30},
            client=client,
        )

        artifacts = _posted_body(client)["input_artifacts"]
        assert artifacts["clipId"] == "clip-9"
        assert artifacts["inputArtifacts"] == {"fps": 30}

    def test_raises_without_backend_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dropping the job silently would lose the upload — fail loudly instead."""
        from queue.job_dispatch import dispatch_on_upload

        monkeypatch.delenv("BACKEND_API_URL", raising=False)
        with pytest.raises(RuntimeError, match="BACKEND_API_URL"):
            dispatch_on_upload(video_id="vid-5", storage_uri="local://raw-video/vid-5.mp4")

    def test_reports_created_false_on_backend_error(self) -> None:
        from queue.job_dispatch import dispatch_on_upload

        client = MagicMock()
        client.post.side_effect = RuntimeError("connection refused")

        result = dispatch_on_upload(
            video_id="vid-6",
            storage_uri="local://raw-video/vid-6.mp4",
            client=client,
        )

        assert result["created"] is False
        assert result["job_id"]
