"""Unit tests for gpu-worker/worker/timeout_handler.py.

All tests run in-process, no GPU hardware or network required.

Covers:
  - Happy path: run_with_timeout returns fn result when fn completes in time
  - Timeout: run_with_timeout raises JobTimeoutError after deadline
  - Timeout: job is marked failed via backend API on timeout
  - Timeout: job is requeued to nightly queue on timeout
  - Failure: exception raised by fn is re-raised by run_with_timeout
  - Failure: backend update failure is swallowed (doesn't mask timeout)
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from worker.timeout_handler import JobTimeoutError, run_with_timeout

# ── Happy path ────────────────────────────────────────────────────────────────


class TestRunWithTimeoutHappyPath:
    def test_returns_fn_result(self) -> None:
        """Fast function returns its value normally."""
        result = run_with_timeout(
            lambda: {"clips": 3},
            job_id="job-1",
            timeout_seconds=5,
        )
        assert result == {"clips": 3}

    def test_passes_args_and_kwargs(self) -> None:
        """Positional and keyword args are forwarded to fn."""
        def adder(a: int, b: int = 0) -> int:
            return a + b

        result = run_with_timeout(
            adder,
            args=(10,),
            kwargs={"b": 5},
            job_id="job-2",
            timeout_seconds=5,
        )
        assert result == 15

    def test_no_backend_call_on_success(self) -> None:
        """Backend API is not touched when the job completes in time."""
        with patch("worker.timeout_handler._update_job_failed") as mock_update:
            run_with_timeout(lambda: None, job_id="job-3", timeout_seconds=5)
            mock_update.assert_not_called()


# ── Timeout ───────────────────────────────────────────────────────────────────


class TestRunWithTimeoutExpiry:
    def test_raises_job_timeout_error(self) -> None:
        """A function that blocks forever triggers JobTimeoutError."""
        barrier = __import__("threading").Event()

        def slow_fn() -> None:
            barrier.wait(timeout=30)

        with pytest.raises(JobTimeoutError):
            run_with_timeout(slow_fn, job_id="job-4", timeout_seconds=1)

        barrier.set()  # allow thread to exit cleanly

    def test_backend_marked_failed_on_timeout(self) -> None:
        """_update_job_failed is called with the timed-out job_id."""
        barrier = __import__("threading").Event()

        def slow_fn() -> None:
            barrier.wait(timeout=30)

        with (
            patch("worker.timeout_handler._update_job_failed") as mock_fail,
            patch("worker.timeout_handler._requeue_for_nightly"),
            pytest.raises(JobTimeoutError),
        ):
            run_with_timeout(slow_fn, job_id="job-5", timeout_seconds=1)

        mock_fail.assert_called_once()
        assert mock_fail.call_args.args[0] == "job-5"
        barrier.set()

    def test_nightly_requeue_called_on_timeout(self) -> None:
        """_requeue_for_nightly is called with the original job payload."""
        barrier = __import__("threading").Event()
        payload = {"jobId": "job-6", "jobType": "render"}

        def slow_fn() -> None:
            barrier.wait(timeout=30)

        with (
            patch("worker.timeout_handler._update_job_failed"),
            patch("worker.timeout_handler._requeue_for_nightly") as mock_requeue,
            pytest.raises(JobTimeoutError),
        ):
            run_with_timeout(
                slow_fn,
                job_id="job-6",
                job_payload=payload,
                timeout_seconds=1,
            )

        mock_requeue.assert_called_once_with("job-6", payload)
        barrier.set()

    def test_no_payload_skips_requeue(self) -> None:
        """When job_payload is None, _requeue_for_nightly is not called."""
        barrier = __import__("threading").Event()

        def slow_fn() -> None:
            barrier.wait(timeout=30)

        with (
            patch("worker.timeout_handler._update_job_failed"),
            patch("worker.timeout_handler._requeue_for_nightly") as mock_requeue,
            pytest.raises(JobTimeoutError),
        ):
            run_with_timeout(
                slow_fn,
                job_id="job-7",
                job_payload=None,
                timeout_seconds=1,
            )

        mock_requeue.assert_not_called()
        barrier.set()

    def test_empty_payload_still_requeues(self) -> None:
        """An explicit empty payload still triggers nightly requeue."""
        barrier = __import__("threading").Event()

        def slow_fn() -> None:
            barrier.wait(timeout=30)

        with (
            patch("worker.timeout_handler._update_job_failed"),
            patch("worker.timeout_handler._requeue_for_nightly") as mock_requeue,
            pytest.raises(JobTimeoutError),
        ):
            run_with_timeout(
                slow_fn,
                job_id="job-7b",
                job_payload={},
                timeout_seconds=1,
            )

        mock_requeue.assert_called_once_with("job-7b", {})
        barrier.set()


# ── Exception propagation ─────────────────────────────────────────────────────


class TestRunWithTimeoutExceptionPropagation:
    def test_fn_exception_is_reraised(self) -> None:
        """An exception inside fn is re-raised (not converted to JobTimeoutError)."""
        def explode() -> None:
            raise ValueError("oops")

        with pytest.raises(ValueError, match="oops"):
            run_with_timeout(explode, job_id="job-8", timeout_seconds=5)

    def test_fn_exception_does_not_call_backend_fail(self) -> None:
        """Backend is NOT marked failed for normal exceptions (caller handles them)."""
        def explode() -> None:
            raise RuntimeError("stage error")

        with (
            patch("worker.timeout_handler._update_job_failed") as mock_fail,
            pytest.raises(RuntimeError),
        ):
            run_with_timeout(explode, job_id="job-9", timeout_seconds=5)

        mock_fail.assert_not_called()


# ── Backend failure robustness ────────────────────────────────────────────────


class TestBackendFailureRobustness:
    def test_backend_update_failure_does_not_mask_timeout(self) -> None:
        """If backend PATCH fails, JobTimeoutError is still raised."""
        import httpx
        barrier = __import__("threading").Event()

        def slow_fn() -> None:
            barrier.wait(timeout=30)

        with (
            patch(
                "worker.timeout_handler._update_job_failed",
                side_effect=httpx.ConnectError("backend down"),
            ),
            patch("worker.timeout_handler._requeue_for_nightly"),
            pytest.raises(JobTimeoutError),
        ):
            run_with_timeout(slow_fn, job_id="job-10", timeout_seconds=1)

        barrier.set()
