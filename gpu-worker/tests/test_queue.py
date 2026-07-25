"""Unit tests for gpu-worker/queue/same_session_queue.py and cf_trigger.py.

All tests run without a real Cloudflare account — HTTP calls are intercepted
with unittest.mock.  No database, no GPU hardware required.

Covers:
  - Happy path: push_same_session_job dispatches to the correct queue
  - Happy path: push_nightly_job dispatches to the nightly queue
  - Happy path: pull_same_session_messages returns parsed messages
  - Happy path: get_same_session_queue_depth returns integer depth
  - Failure: HTTP error from CF API propagates as httpx.HTTPStatusError
  - Failure: get_same_session_queue_depth returns 0 on connection error
  - cf_trigger.dispatch_on_upload uses same-session queue when is_same_session=True
  - cf_trigger.dispatch_on_upload uses nightly queue when is_same_session=False
  - cf_trigger.dispatch_on_upload sets correct priority on payload
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# Ensure gpu-worker root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _enable_cf_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise the CF transport, which is opt-in since the
    DB-as-queue migration — the default (unset) skips CF publishes."""
    monkeypatch.setenv("CF_QUEUES_ENABLED", "1")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_push_response(message_id: str = "msg-abc") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "result": {"messages": [{"id": message_id}]}
    }
    return resp


def _mock_pull_response(messages: list[dict[str, Any]]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"result": {"messages": messages}}
    return resp


def _mock_depth_response(total: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"result": {"stats": {"totalMessages": total}}}
    return resp


# ── same_session_queue tests ──────────────────────────────────────────────────


class TestPushSameSessionJob:
    def test_happy_path_returns_message_id(self) -> None:
        """push_same_session_job returns the CF message ID on success."""
        from queue.same_session_queue import SAME_SESSION_QUEUE, push_same_session_job

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_push_response("msg-123")

        msg_id = push_same_session_job(
            {"jobId": "j1", "jobType": "ingest"},
            client=mock_client,
        )

        assert msg_id == "msg-123"
        call_args = mock_client.post.call_args
        assert SAME_SESSION_QUEUE in call_args.args[0]

    def test_payload_sent_as_json(self) -> None:
        """The job payload is embedded in the messages array."""
        import json
        from queue.same_session_queue import push_same_session_job

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_push_response()

        push_same_session_job({"jobId": "j2", "jobType": "render"}, client=mock_client)

        raw = mock_client.post.call_args.kwargs.get("content") or \
              mock_client.post.call_args[1].get("content")
        body = json.loads(raw)
        assert body["messages"][0]["body"]["jobId"] == "j2"

    def test_http_error_propagates(self) -> None:
        """An HTTP 500 from CF API raises httpx.HTTPStatusError."""
        from queue.same_session_queue import push_same_session_job

        import httpx

        mock_client = MagicMock()
        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        mock_client.post.return_value = err_resp

        with pytest.raises(httpx.HTTPStatusError):
            push_same_session_job({"jobId": "j3"}, client=mock_client)


class TestPushNightlyJob:
    def test_happy_path_uses_nightly_queue(self) -> None:
        """push_nightly_job routes to the nightly video-processing queue."""
        from queue.same_session_queue import NIGHTLY_QUEUE, push_nightly_job

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_push_response("msg-nightly")

        msg_id = push_nightly_job({"jobId": "n1"}, client=mock_client)

        assert msg_id == "msg-nightly"
        url = mock_client.post.call_args.args[0]
        assert NIGHTLY_QUEUE in url


class TestPullSameSessionMessages:
    def test_happy_path_returns_messages(self) -> None:
        """pull_same_session_messages returns the parsed message list."""
        from queue.same_session_queue import pull_same_session_messages

        expected = [{"lease_id": "lease1", "body": {"jobId": "j1"}}]
        mock_client = MagicMock()
        mock_client.post.return_value = _mock_pull_response(expected)

        msgs = pull_same_session_messages(batch_size=2, client=mock_client)

        assert msgs == expected

    def test_empty_queue_returns_empty_list(self) -> None:
        from queue.same_session_queue import pull_same_session_messages

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_pull_response([])

        msgs = pull_same_session_messages(client=mock_client)
        assert msgs == []

    def test_http_error_propagates(self) -> None:
        from queue.same_session_queue import pull_same_session_messages

        import httpx

        mock_client = MagicMock()
        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock()
        )
        mock_client.post.return_value = err_resp

        with pytest.raises(httpx.HTTPStatusError):
            pull_same_session_messages(client=mock_client)


class TestQueueDepth:
    def test_happy_path_returns_depth(self) -> None:
        from queue.same_session_queue import get_same_session_queue_depth

        mock_client = MagicMock()
        mock_client.get.return_value = _mock_depth_response(7)

        depth = get_same_session_queue_depth(client=mock_client)
        assert depth == 7

    def test_returns_zero_on_connection_error(self) -> None:
        """Network failure degrades gracefully to 0."""
        from queue.same_session_queue import get_same_session_queue_depth

        mock_client = MagicMock()
        mock_client.get.side_effect = ConnectionError("offline")

        depth = get_same_session_queue_depth(client=mock_client)
        assert depth == 0


# ── cf_trigger tests ──────────────────────────────────────────────────────────


class TestDispatchOnUpload:
    def test_same_session_uses_same_session_queue(self) -> None:
        """Same-session uploads go to the same-session queue with priority 10."""
        from queue.cf_trigger import dispatch_on_upload
        from queue.same_session_queue import SAME_SESSION_PRIORITY

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_push_response("msg-ss")

        result = dispatch_on_upload(
            video_id="vid-1",
            storage_uri="r2://football-iq/videos/vid-1.mp4",
            is_same_session=True,
            client=mock_client,
        )

        assert result["priority"] == SAME_SESSION_PRIORITY
        assert "same-session" in result["queue"]

    def test_nightly_uses_nightly_queue(self) -> None:
        """Non-same-session uploads go to nightly queue with priority 0."""
        from queue.cf_trigger import dispatch_on_upload
        from queue.same_session_queue import NIGHTLY_PRIORITY

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_push_response("msg-nightly")

        result = dispatch_on_upload(
            video_id="vid-2",
            storage_uri="r2://football-iq/videos/vid-2.mp4",
            is_same_session=False,
            client=mock_client,
        )

        assert result["priority"] == NIGHTLY_PRIORITY
        assert "video-processing" in result["queue"]

    def test_returns_job_id_and_message_id(self) -> None:
        """Return dict contains both job_id and message_id."""
        from queue.cf_trigger import dispatch_on_upload

        mock_client = MagicMock()
        mock_client.post.return_value = _mock_push_response("msg-xyz")

        result = dispatch_on_upload(
            video_id="vid-3",
            storage_uri="r2://football-iq/videos/vid-3.mp4",
            client=mock_client,
        )

        assert "job_id" in result
        assert result["message_id"] == "msg-xyz"

    def test_push_failure_propagates(self) -> None:
        """HTTP error during queue push is raised to the caller."""
        from queue.cf_trigger import dispatch_on_upload

        import httpx

        mock_client = MagicMock()
        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        mock_client.post.return_value = err_resp

        with pytest.raises(httpx.HTTPStatusError):
            dispatch_on_upload(
                video_id="vid-4",
                storage_uri="r2://football-iq/videos/vid-4.mp4",
                is_same_session=True,
                client=mock_client,
            )
