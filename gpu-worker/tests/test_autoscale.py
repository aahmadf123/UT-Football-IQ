"""Unit tests for gpu-worker/worker/autoscale.py.

All tests run without real Cloudflare accounts or GPU infrastructure.
HTTP calls are intercepted with unittest.mock.

Covers:
  - Happy path: request_burst calls the scale API with scale_up action
  - Happy path: request_scale_down calls the scale API with scale_down action
  - Happy path: both return True in dry-run mode (no AUTOSCALE_API_URL)
  - Failure: API error returns False without raising
  - run_autoscale_loop triggers burst when queue depth > 0
  - run_autoscale_loop scales down when queue drains
  - run_autoscale_loop handles queue depth errors gracefully
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_scale_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    return resp


def _err_scale_response() -> MagicMock:
    import httpx
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    return resp


# ── request_burst ─────────────────────────────────────────────────────────────


class TestRequestBurst:
    def test_happy_path_calls_scale_api(self) -> None:
        """request_burst POSTs scale_up to the autoscale API."""
        with patch.dict(os.environ, {"AUTOSCALE_API_URL": "http://scale.test/api"}):
            # Re-import to pick up env var
            import importlib

            import worker.autoscale as autoscale_mod
            importlib.reload(autoscale_mod)

            mock_client = MagicMock()
            mock_client.post.return_value = _ok_scale_response()

            result = autoscale_mod.request_burst(instances=2, client=mock_client)

            assert result is True
            payload = mock_client.post.call_args.kwargs["json"]
            assert payload["action"] == "scale_up"
            assert payload["instances"] == 2

    def test_dry_run_returns_true_without_http(self) -> None:
        """No API URL → dry-run mode returns True without any HTTP call."""
        with patch.dict(os.environ, {"AUTOSCALE_API_URL": ""}):
            import importlib

            import worker.autoscale as autoscale_mod
            importlib.reload(autoscale_mod)

            mock_client = MagicMock()
            result = autoscale_mod.request_burst(client=mock_client)

            assert result is True
            mock_client.post.assert_not_called()

    def test_api_error_returns_false(self) -> None:
        """HTTP error from scale API returns False (does not raise)."""
        with patch.dict(os.environ, {"AUTOSCALE_API_URL": "http://scale.test/api"}):
            import importlib

            import worker.autoscale as autoscale_mod
            importlib.reload(autoscale_mod)

            mock_client = MagicMock()
            mock_client.post.return_value = _err_scale_response()

            result = autoscale_mod.request_burst(client=mock_client)
            assert result is False


# ── request_scale_down ────────────────────────────────────────────────────────


class TestRequestScaleDown:
    def test_happy_path_calls_scale_down(self) -> None:
        """request_scale_down POSTs scale_down action."""
        with patch.dict(os.environ, {"AUTOSCALE_API_URL": "http://scale.test/api"}):
            import importlib

            import worker.autoscale as autoscale_mod
            importlib.reload(autoscale_mod)

            mock_client = MagicMock()
            mock_client.post.return_value = _ok_scale_response()

            result = autoscale_mod.request_scale_down(client=mock_client)

            assert result is True
            payload = mock_client.post.call_args.kwargs["json"]
            assert payload["action"] == "scale_down"

    def test_dry_run_returns_true(self) -> None:
        with patch.dict(os.environ, {"AUTOSCALE_API_URL": ""}):
            import importlib

            import worker.autoscale as autoscale_mod
            importlib.reload(autoscale_mod)

            result = autoscale_mod.request_scale_down()
            assert result is True

    def test_api_error_returns_false(self) -> None:
        with patch.dict(os.environ, {"AUTOSCALE_API_URL": "http://scale.test/api"}):
            import importlib

            import worker.autoscale as autoscale_mod
            importlib.reload(autoscale_mod)

            mock_client = MagicMock()
            mock_client.post.return_value = _err_scale_response()

            result = autoscale_mod.request_scale_down(client=mock_client)
            assert result is False


# ── run_autoscale_loop ────────────────────────────────────────────────────────


class TestAutoscaleLoop:
    def test_burst_triggered_when_queue_non_empty(self) -> None:
        """Loop calls request_burst when same-session queue depth > 0."""
        import importlib

        import worker.autoscale as autoscale_mod
        importlib.reload(autoscale_mod)

        call_log: list[str] = []

        def fake_depth(**_kwargs: object) -> int:
            return 3

        def fake_burst(**_kwargs: object) -> bool:
            call_log.append("burst")
            return True

        def fake_scale_down(**_kwargs: object) -> bool:  # pragma: no cover
            call_log.append("scale_down")
            return True

        autoscale_mod._shutdown = False  # type: ignore[attr-defined]

        # Patch depth and scale calls; stop loop after one iteration
        with (
            patch.object(autoscale_mod, "get_same_session_queue_depth", fake_depth),
            patch.object(autoscale_mod, "request_burst", fake_burst),
            patch.object(autoscale_mod, "request_scale_down", fake_scale_down),
            patch("time.sleep", side_effect=StopIteration),  # break after 1 iter
        ):
            try:
                autoscale_mod.run_autoscale_loop()
            except StopIteration:
                pass

        assert "burst" in call_log

    def test_scale_down_triggered_when_queue_drains(self) -> None:
        """Loop calls request_scale_down when depth returns to 0 after burst."""
        import importlib

        import worker.autoscale as autoscale_mod
        importlib.reload(autoscale_mod)

        call_log: list[str] = []
        depths = iter([2, 0])

        def fake_depth(**_kwargs: object) -> int:
            try:
                return next(depths)
            except StopIteration:
                return 0

        def fake_burst(**_kwargs: object) -> bool:
            call_log.append("burst")
            return True

        def fake_scale_down(**_kwargs: object) -> bool:
            call_log.append("scale_down")
            autoscale_mod._shutdown = True  # type: ignore[attr-defined]
            return True

        autoscale_mod._shutdown = False  # type: ignore[attr-defined]

        with (
            patch.object(autoscale_mod, "get_same_session_queue_depth", fake_depth),
            patch.object(autoscale_mod, "request_burst", fake_burst),
            patch.object(autoscale_mod, "request_scale_down", fake_scale_down),
            patch("time.sleep", return_value=None),
        ):
            autoscale_mod.run_autoscale_loop()

        assert "burst" in call_log
        assert "scale_down" in call_log

    def test_loop_handles_depth_error_gracefully(self) -> None:
        """Queue depth exception does not crash the loop."""
        import importlib

        import worker.autoscale as autoscale_mod
        importlib.reload(autoscale_mod)

        def boom(**_kwargs: object) -> int:
            raise RuntimeError("cf api down")

        with (
            patch.object(autoscale_mod, "get_same_session_queue_depth", boom),
            patch("time.sleep", side_effect=StopIteration),
        ):
            try:
                autoscale_mod.run_autoscale_loop()
            except StopIteration:
                pass
        # No exception leaked — test passes if we get here
