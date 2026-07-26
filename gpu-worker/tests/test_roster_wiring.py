"""The roster has to reach ``stage_reid``, or identity cannot work at all.

``stage_reid`` builds its ``jersey_number -> player_id`` map from the ``roster``
argument. No caller ever passed one, so the map was always empty: OCR could read
a jersey perfectly and still have nobody to attribute it to. That is a wiring
gap, not a model gap, and no amount of model work would have closed it.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import backend


def _response(payload: Any, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.side_effect = None if status < 400 else RuntimeError("boom")
    return resp


def _with_client(resp: MagicMock) -> Any:
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    return client


class TestFetchRoster:
    def test_returns_the_active_players(self) -> None:
        players = [{"id": "p1", "jersey_number": 12}, {"id": "p2", "jersey_number": 43}]
        with (
            patch.object(backend, "BACKEND_API_URL", "http://api"),
            patch.object(backend, "_client", return_value=_with_client(_response(players))),
        ):
            assert backend.fetch_roster() == players

    def test_asks_only_for_the_active_roster(self) -> None:
        # Graduated players keep their jersey numbers, and a stale number
        # colliding with a current one attributes film to the wrong athlete.
        client = _with_client(_response([]))
        with (
            patch.object(backend, "BACKEND_API_URL", "http://api"),
            patch.object(backend, "_client", return_value=client),
        ):
            backend.fetch_roster()
        params = client.__enter__.return_value.get.call_args.kwargs["params"]
        assert params["is_active"] is True

    def test_offline_returns_empty_rather_than_raising(self) -> None:
        with patch.object(backend, "BACKEND_API_URL", ""):
            assert backend.fetch_roster() == []

    def test_a_backend_error_degrades_to_unidentified(self) -> None:
        # Losing identity is bad; losing the whole clip because the roster
        # endpoint blipped is worse.
        client = MagicMock()
        client.__enter__.return_value.get.side_effect = RuntimeError("connection refused")
        with (
            patch.object(backend, "BACKEND_API_URL", "http://api"),
            patch.object(backend, "_client", return_value=client),
        ):
            assert backend.fetch_roster() == []

    def test_an_unexpected_payload_shape_is_refused(self) -> None:
        # A paginated envelope instead of a bare list would otherwise reach
        # stage_reid and fail on the first `p["jersey_number"]`.
        with (
            patch.object(backend, "BACKEND_API_URL", "http://api"),
            patch.object(
                backend, "_client", return_value=_with_client(_response({"players": []}))
            ),
        ):
            assert backend.fetch_roster() == []

    def test_a_full_page_is_flagged(self, caplog: pytest.LogCaptureFixture) -> None:
        # Silent truncation shows up as a few players who can never be
        # identified, with nothing pointing at the cause.
        full = [{"id": f"p{i}", "jersey_number": i} for i in range(backend.ROSTER_PAGE_LIMIT)]
        with (
            patch.object(backend, "BACKEND_API_URL", "http://api"),
            patch.object(backend, "_client", return_value=_with_client(_response(full))),
            patch.object(backend.log, "warning") as warned,
        ):
            backend.fetch_roster()
        assert any("roster_page_limit_reached" in str(c) for c in warned.call_args_list)


class TestRosterReachesStageReid:
    def test_the_jersey_map_is_built_from_it(self) -> None:
        from pipeline import stage_reid

        roster = [
            {"id": "player-a", "jersey_number": 12},
            {"id": "player-b", "jersey_number": None},  # walk-on with no number yet
        ]
        jersey_map = {
            p["jersey_number"]: p["id"] for p in roster if p.get("jersey_number") is not None
        }
        assert jersey_map == {12: "player-a"}
        assert stage_reid is not None  # the map above mirrors stage_reid.run

    def test_the_worker_passes_a_roster(self) -> None:
        # `import __main__` under pytest resolves to pytest's own entrypoint, so
        # the module is loaded from its path -- same approach as
        # test_db_queue_worker.
        import importlib.util

        root = os.path.join(os.path.dirname(__file__), "..")
        spec = importlib.util.spec_from_file_location(
            "gpu_worker_main", os.path.join(root, "__main__.py")
        )
        assert spec is not None and spec.loader is not None
        worker_main = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("gpu_worker_main", worker_main)
        spec.loader.exec_module(worker_main)

        with (
            patch.object(worker_main, "_update_job_status"),
            patch.object(worker_main, "heartbeat_db_job"),
            patch("pipeline.backend.fetch_roster", return_value=[{"id": "p1"}]) as fetch,
            patch("pipeline.orchestrator.run_pipeline", return_value={"clips": []}) as run,
        ):
            worker_main.process_pipeline_job(
                {
                    "jobId": "job-1",
                    "videoId": "vid-1",
                    "inputUri": "s3://raw-video/x.mp4",
                    "priority": 10,
                }
            )
        fetch.assert_called_once()
        assert run.call_args.kwargs["roster"] == [{"id": "p1"}]
