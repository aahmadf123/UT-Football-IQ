"""Field coordinates reach the track points, and the stage order that needs them.

Together these cover the seam that made the pipeline produce zeros on real film:
the calibration was computed, POSTed, and then dropped, so nothing ever wrote
``field_x``. Because every reader guards with ``.get("field_x")`` and falls back
to ``or 0``, the failure was completely silent — metrics came out, they were
just meaningless.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from pipeline import stage_track
from pipeline.lightweight_config import NIGHTLY_STAGES, SAME_SESSION_STAGES
from pipeline.orchestrator import CLIP_STAGES, VIDEO_STAGES

# Scale pixels by 1/10 and shift, so expected yards are easy to read off.
SCALED_H = [0.1, 0.0, 0.0, 0.0, 0.1, -20.0, 0.0, 0.0, 1.0]
IDENTITY_H = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


class _FakeTrack:
    def __init__(self, points: list[dict[str, Any]]) -> None:
        self.track_id = "t1"
        self.points = points
        self.start_frame = points[0]["frame_number"] if points else 0
        self.last_frame = points[-1]["frame_number"] if points else 0


class _FakeTracker:
    mask_aware = False

    def __init__(self, tracks: list[_FakeTrack]) -> None:
        self._tracks = tracks

    def track(self, detections: dict[str, Any]) -> list[_FakeTrack]:
        return self._tracks


def _points(count: int = 3) -> list[dict[str, Any]]:
    return [
        {"frame_number": i, "bbox": [100.0 + i * 10, 50.0, 140.0 + i * 10, 130.0]}
        for i in range(count)
    ]


def _run(homography: list[float] | None, analytics_safe: bool) -> dict[str, Any]:
    track = _FakeTrack(_points())
    with patch.object(stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}):
        result = stage_track.run(
            "clip-1",
            {},
            30.0,
            "job-1",
            tracker=_FakeTracker([track]),
            homography=homography,
            analytics_safe=analytics_safe,
        )
    return {"result": result, "points": track.points}


class TestTrackPointProjection:
    def test_writes_field_coordinates(self) -> None:
        out = _run(SCALED_H, analytics_safe=True)
        for point in out["points"]:
            assert point["field_x"] is not None
            assert point["field_y"] is not None

    def test_projects_the_ground_anchor_not_the_box_centre(self) -> None:
        # bbox [100, 50, 140, 130] -> feet at (120, 130) -> (12.0, -7.0).
        # The box centre (120, 90) would give y = -11.0, i.e. four yards off.
        out = _run(SCALED_H, analytics_safe=True)
        first = out["points"][0]
        assert first["field_x"] == pytest.approx(12.0)
        assert first["field_y"] == pytest.approx(-7.0)

    def test_reports_how_many_points_it_projected(self) -> None:
        assert _run(SCALED_H, analytics_safe=True)["result"]["projected_points"] == 3

    def test_writes_nothing_when_calibration_is_not_analytics_safe(self) -> None:
        # A homography nobody trusts produces plausible-looking yard values that
        # are wrong, which is worse than none: the readers treat a missing key
        # as "no spatial metrics" and suppress, but a present-and-wrong value
        # flows into separation, speed and workload as though it were real.
        out = _run(SCALED_H, analytics_safe=False)
        assert out["result"]["projected_points"] == 0
        for point in out["points"]:
            assert "field_x" not in point

    def test_writes_nothing_without_a_homography(self) -> None:
        out = _run(None, analytics_safe=True)
        assert out["result"]["projected_points"] == 0
        for point in out["points"]:
            assert "field_x" not in point

    def test_survives_an_unusable_homography(self) -> None:
        # Calibration can emit a degenerate fit; the clip must still track.
        out = _run([1.0, 2.0, 3.0], analytics_safe=True)
        assert out["result"]["projected_points"] == 0
        assert out["result"]["tracklet_count"] == 1

    def test_the_backend_receives_the_coordinates(self) -> None:
        # The same point dicts go to create_tracklet and to the in-memory
        # tracklets, so mutating in place is what makes both carry them.
        track = _FakeTrack(_points())
        with patch.object(
            stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}
        ) as create:
            stage_track.run(
                "clip-1",
                {},
                30.0,
                "job-1",
                tracker=_FakeTracker([track]),
                homography=SCALED_H,
                analytics_safe=True,
            )
        sent = create.call_args.kwargs["track_points"]
        assert all("field_x" in p for p in sent)

    def test_points_without_a_bbox_are_skipped(self) -> None:
        track = _FakeTrack([{"frame_number": 0}, *_points(2)])
        with patch.object(stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}):
            result = stage_track.run(
                "clip-1",
                {},
                30.0,
                "job-1",
                tracker=_FakeTracker([track]),
                homography=IDENTITY_H,
                analytics_safe=True,
            )
        assert result["projected_points"] == 2
        assert "field_x" not in track.points[0]


class TestStageOrder:
    """The run order lives in lightweight_config, not in CLIP_STAGES.

    ``run_pipeline`` filters its stage list with ``[s for s in stage_list if s
    in CLIP_STAGES]`` — membership only. Reordering CLIP_STAGES alone changes
    nothing at runtime, which is exactly the kind of edit that looks correct in
    review and does nothing.
    """

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_events_precede_pose(self, name: str, stages: list[str]) -> None:
        # stage_pose takes the events list and anchors its biomechanics at the
        # snap. With pose first it was always handed [], so every pose metric
        # was computed against no snap at all.
        assert stages.index("events") < stages.index("pose"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_events_precede_labels(self, name: str, stages: list[str]) -> None:
        # stage_labels reads formation at the snap frame.
        assert stages.index("events") < stages.index("labels"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_track_precedes_events(self, name: str, stages: list[str]) -> None:
        assert stages.index("track") < stages.index("events"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_calibrate_precedes_track(self, name: str, stages: list[str]) -> None:
        # Track projects field coordinates, so it needs the homography.
        assert stages.index("calibrate") < stages.index("track"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_canonical_order_agrees_with_the_run_lists(self, name: str, stages: list[str]) -> None:
        """CLIP_STAGES documents the order; the run lists decide it.

        They are edited in different files, so without this they drift and the
        documented order becomes a comfortable lie.
        """
        clip_only = [s for s in stages if s in CLIP_STAGES]
        expected = [s for s in CLIP_STAGES if s in stages]
        assert clip_only == expected, name

    def test_video_stages_are_disjoint_from_clip_stages(self) -> None:
        assert not set(VIDEO_STAGES) & set(CLIP_STAGES)
